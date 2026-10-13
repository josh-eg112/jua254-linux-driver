/*
 * JUA254 GPU capture helper: ext-image-copy dmabuf capture of one Wayland
 * output, VA-API colour conversion and baseline JPEG encode, damage tiles,
 * and an optional separate pointer-cursor stream for the adapter's hardware
 * cursor. No pixel data passes through the CPU on the frame path.
 *
 * stdout carries binary records (struct rec + payload); stderr carries one
 * JSON diagnostics object per line. stdin accepts single-byte commands:
 * 'F' forces the next frame to be a full frame.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>
#include <drm_fourcc.h>
#include <gbm.h>
#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>
#include <xf86drm.h>
#include <wayland-client.h>
#include "ext-image-capture-source-v1-client-protocol.h"
#include "ext-image-copy-capture-v1-client-protocol.h"
#include "linux-dmabuf-v1-client-protocol.h"
#include "jpeg_tables.h"

#define SLOTS 2
#define MAX_DAMAGE 64
#define MAX_TILES 512

enum { REC_FULL = 1, REC_TILE = 2, REC_FRAME_END = 3, REC_CURSOR_SHAPE = 4, REC_CURSOR_POS = 5, REC_CURSOR_LEAVE = 6 };

struct rec {
    char magic[4];
    uint32_t kind, payload_len, seq;
    int32_t x, y;
    uint32_t w, h;
    uint64_t requested_ns, ready_ns;
} __attribute__((packed));

struct rect { int32_t x, y, w, h; };
struct output_info { struct wl_output *object; char name[128]; };

struct slot {
    struct gbm_bo *bo; struct wl_buffer *buffer; VASurfaceID surface;
    int fd; uint32_t offset, stride; uint64_t modifier;
};

struct frame_state {
    struct ext_image_copy_capture_frame_v1 *frame; int slot;
    int done, failed; uint32_t fail_reason;
    struct rect damage[MAX_DAMAGE]; int damage_count, damage_overflow;
    uint64_t requested_ns, ready_ns, presentation_ns;
};

struct encoder {
    int w, h; VAConfigID config; VAContextID ctx; VASurfaceID nv12; VABufferID coded;
    uint8_t header[1024]; size_t header_len;
};

struct cursor {
    struct ext_image_copy_capture_cursor_session_v1 *session;
    struct ext_image_copy_capture_session_v1 *capture;
    struct ext_image_copy_capture_frame_v1 *frame;
    uint32_t width, height, pending_w, pending_h; int constraints_done, shm_argb;
    struct wl_buffer *buffer; void *pixels; size_t size;
    int32_t x, y, hot_x, hot_y, pending_hot_x, pending_hot_y; int inside, pos_dirty, leave_dirty;
    int frame_done, frame_failed;
};

struct app {
    struct wl_display *display; struct wl_shm *shm; struct wl_seat *seat; struct wl_pointer *pointer;
    struct zwp_linux_dmabuf_v1 *dmabuf;
    struct ext_output_image_capture_source_manager_v1 *source_manager;
    struct ext_image_copy_capture_manager_v1 *capture_manager;
    struct ext_image_copy_capture_session_v1 *session;
    struct output_info outputs[32]; int output_count;
    uint32_t width, height; int constraints_done, stopped;
    dev_t dmabuf_dev; int have_dmabuf_dev;
    uint32_t fmt_xrgb_count, fmt_argb_count; uint64_t mods_xrgb[64], mods_argb[64];
    struct slot slots[SLOTS];
    struct frame_state inflight; int have_inflight;
    struct cursor cursor; int hw_cursor;
    VADisplay va; VAConfigID vpp_config; VAContextID vpp_ctx;
    struct encoder full, tile; int quality, full_range;
    int tile_w, tile_h, max_tiles; double full_threshold; int damage_tiles;
    int force_full; uint32_t seq; int frame_timeout_ms; int probe;
    uint64_t tiles_sent, full_sent, frames;
};

static uint64_t now_ns(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec; }
static double ms_since(uint64_t start) { return (now_ns() - start) / 1e6; }
static void die(const char *what) { fprintf(stderr, "{\"event\":\"fatal\",\"error\":\"%s\",\"errno\":%d}\n", what, errno); exit(3); }
static void va_check(VAStatus s, const char *what) { if (s != VA_STATUS_SUCCESS) { fprintf(stderr, "{\"event\":\"fatal\",\"error\":\"%s\",\"va\":\"%s\"}\n", what, vaErrorStr(s)); exit(4); } }

static int write_all(const void *buf, size_t size) {
    const uint8_t *p = buf;
    while (size) { ssize_t n = write(STDOUT_FILENO, p, size); if (n < 0) { if (errno == EINTR) continue; return -1; } p += n; size -= n; }
    return 0;
}
static void emit(struct app *a, uint32_t kind, int32_t x, int32_t y, uint32_t w, uint32_t h, const void *payload, uint32_t len, uint64_t req, uint64_t ready) {
    struct rec r = { {'J','G','R','1'}, kind, len, a->seq, x, y, w, h, req, ready };
    if (write_all(&r, sizeof r) < 0 || (len && write_all(payload, len) < 0)) exit(0); /* reader went away */
}

/* ---------------- JPEG header (mirrors FFmpeg mjpeg_vaapi -jfif 1, IDs 0/1/2) ---------------- */

static void quant_tables(int quality, uint8_t lum[64], uint8_t chr[64]) {
    int scale = quality < 50 ? 5000 / quality : 200 - 2 * quality;
    for (int i = 0; i < 64; i++) {
        int l = jt_quant_luminance[i] * scale / 100, c = jt_quant_chrominance[i] * scale / 100;
        lum[i] = l < 1 ? 1 : l > 255 ? 255 : l; chr[i] = c < 1 ? 1 : c > 255 ? 255 : c;
    }
}
static size_t put16(uint8_t *p, int v) { p[0] = v >> 8; p[1] = v; return 2; }
static size_t huff(uint8_t *p, int cls_id, const uint8_t *bits17, const uint8_t *vals) {
    size_t n = 0; int k = 0; p[n++] = cls_id;
    for (int i = 0; i < 16; i++) { p[n++] = bits17[i + 1]; k += bits17[i + 1]; }
    memcpy(p + n, vals, k); return n + k;
}
static size_t jpeg_header(uint8_t *p, int w, int h, int quality) {
    uint8_t lum[64], chr[64]; size_t n = 0, len_at;
    quant_tables(quality, lum, chr);
    p[n++] = 0xFF; p[n++] = 0xD8;
    static const uint8_t app0[] = { 0xFF, 0xE0, 0, 16, 'J','F','I','F',0, 1,2, 0, 0,1, 0,1, 0,0 };
    memcpy(p + n, app0, sizeof app0); n += sizeof app0;
    p[n++] = 0xFF; p[n++] = 0xDB; n += put16(p + n, 2 + 65 * 2);
    p[n++] = 0x00; memcpy(p + n, lum, 64); n += 64;
    p[n++] = 0x01; memcpy(p + n, chr, 64); n += 64;
    p[n++] = 0xFF; p[n++] = 0xC0; n += put16(p + n, 17); p[n++] = 8; n += put16(p + n, h); n += put16(p + n, w); p[n++] = 3;
    p[n++] = 0; p[n++] = 0x22; p[n++] = 0;  p[n++] = 1; p[n++] = 0x11; p[n++] = 1;  p[n++] = 2; p[n++] = 0x11; p[n++] = 1;
    p[n++] = 0xFF; p[n++] = 0xC4; len_at = n; n += 2;
    n += huff(p + n, 0x00, jt_bits_dc_luminance, jt_val_dc);
    n += huff(p + n, 0x10, jt_bits_ac_luminance, jt_val_ac_luminance);
    n += huff(p + n, 0x01, jt_bits_dc_chrominance, jt_val_dc);
    n += huff(p + n, 0x11, jt_bits_ac_chrominance, jt_val_ac_chrominance);
    put16(p + len_at, (int)(n - len_at));
    static const uint8_t sos[] = { 0xFF, 0xDA, 0, 12, 3, 0, 0x00, 1, 0x11, 2, 0x11, 0, 63, 0 };
    memcpy(p + n, sos, sizeof sos); n += sizeof sos;
    return n;
}

/* ---------------- VA-API ---------------- */

static void encoder_init(struct app *a, struct encoder *e, int w, int h) {
    e->w = w; e->h = h;
    VAConfigAttrib attr = { .type = VAConfigAttribRTFormat, .value = VA_RT_FORMAT_YUV420 };
    va_check(vaCreateConfig(a->va, VAProfileJPEGBaseline, VAEntrypointEncPicture, &attr, 1, &e->config), "jpeg config");
    va_check(vaCreateSurfaces(a->va, VA_RT_FORMAT_YUV420, w, h, &e->nv12, 1, NULL, 0), "nv12 surface");
    va_check(vaCreateContext(a->va, e->config, w, h, VA_PROGRESSIVE, &e->nv12, 1, &e->ctx), "jpeg context");
    va_check(vaCreateBuffer(a->va, e->ctx, VAEncCodedBufferType, w * h * 3 + 65536, 1, NULL, &e->coded), "coded buffer");
    e->header_len = jpeg_header(e->header, w, h, a->quality);
}

static VASurfaceID import_dmabuf(struct app *a, struct slot *s, uint32_t drm_format) {
    VADRMPRIMESurfaceDescriptor d = {0};
    d.fourcc = drm_format == DRM_FORMAT_ARGB8888 ? VA_FOURCC_BGRA : VA_FOURCC_BGRX;
    d.width = a->width; d.height = a->height; d.num_objects = 1;
    d.objects[0].fd = s->fd; d.objects[0].size = s->offset + s->stride * a->height; d.objects[0].drm_format_modifier = s->modifier;
    d.num_layers = 1; d.layers[0].drm_format = drm_format; d.layers[0].num_planes = 1;
    d.layers[0].object_index[0] = 0; d.layers[0].offset[0] = s->offset; d.layers[0].pitch[0] = s->stride;
    VASurfaceAttrib attrs[2] = {
        { .type = VASurfaceAttribMemoryType, .flags = VA_SURFACE_ATTRIB_SETTABLE, .value = { .type = VAGenericValueTypeInteger, .value.i = VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2 } },
        { .type = VASurfaceAttribExternalBufferDescriptor, .flags = VA_SURFACE_ATTRIB_SETTABLE, .value = { .type = VAGenericValueTypePointer, .value.p = &d } },
    };
    VASurfaceID id;
    va_check(vaCreateSurfaces(a->va, VA_RT_FORMAT_RGB32, a->width, a->height, &id, 1, attrs, 2), "dmabuf import");
    return id;
}

/* Convert a region of the captured RGB surface into the encoder's NV12 surface. */
static void vpp(struct app *a, VASurfaceID src, struct encoder *e, const VARectangle *region) {
    VAProcPipelineParameterBuffer p = {0}; VABufferID buf;
    p.surface = src; p.surface_region = region; p.output_region = NULL;
    p.output_background_color = 0xff000000;
    p.surface_color_standard = VAProcColorStandardNone;
    p.output_color_standard = VAProcColorStandardBT601;
    p.input_color_properties.color_range = VA_SOURCE_RANGE_FULL;
    p.output_color_properties.color_range = a->full_range ? VA_SOURCE_RANGE_FULL : VA_SOURCE_RANGE_REDUCED;
    p.filter_flags = VA_FILTER_SCALING_FAST;
    va_check(vaCreateBuffer(a->va, a->vpp_ctx, VAProcPipelineParameterBufferType, sizeof p, 1, &p, &buf), "vpp params");
    va_check(vaBeginPicture(a->va, a->vpp_ctx, e->nv12), "vpp begin");
    va_check(vaRenderPicture(a->va, a->vpp_ctx, &buf, 1), "vpp render");
    va_check(vaEndPicture(a->va, a->vpp_ctx), "vpp end");
    vaDestroyBuffer(a->va, buf);
}

/* Encode the encoder's NV12 surface; returns a malloc'd complete JPEG. */
static uint8_t *encode(struct app *a, struct encoder *e, size_t *out_len) {
    VAEncPictureParameterBufferJPEG pic = {0};
    pic.reconstructed_picture = e->nv12; pic.coded_buf = e->coded;
    pic.picture_width = e->w; pic.picture_height = e->h;
    pic.pic_flags.bits.huffman = 1; pic.sample_bit_depth = 8; pic.num_scan = 1; pic.num_components = 3;
    pic.quality = 50; /* driver identity scale: the tables below are used unchanged */
    for (int i = 0; i < 3; i++) { pic.component_id[i] = i; pic.quantiser_table_selector[i] = i > 0; }
    VAQMatrixBufferJPEG q = {0}; q.load_lum_quantiser_matrix = 1; q.load_chroma_quantiser_matrix = 1;
    quant_tables(a->quality, q.lum_quantiser_matrix, q.chroma_quantiser_matrix);
    VAHuffmanTableBufferJPEGBaseline hu = {0};
    for (int t = 0; t < 2; t++) {
        const uint8_t *dcb = t ? jt_bits_dc_chrominance : jt_bits_dc_luminance, *acb = t ? jt_bits_ac_chrominance : jt_bits_ac_luminance;
        const uint8_t *acv = t ? jt_val_ac_chrominance : jt_val_ac_luminance; int k;
        hu.load_huffman_table[t] = 1;
        k = 0; for (int i = 0; i < 16; i++) k += (hu.huffman_table[t].num_dc_codes[i] = dcb[i + 1]);
        memcpy(hu.huffman_table[t].dc_values, jt_val_dc, k);
        k = 0; for (int i = 0; i < 16; i++) k += (hu.huffman_table[t].num_ac_codes[i] = acb[i + 1]);
        memcpy(hu.huffman_table[t].ac_values, acv, k);
    }
    VAEncSliceParameterBufferJPEG sl = {0}; sl.num_components = 3;
    for (int i = 0; i < 3; i++) { sl.components[i].component_selector = i; sl.components[i].dc_table_selector = i > 0; sl.components[i].ac_table_selector = i > 0; }
    VAEncPackedHeaderParameterBuffer ph = { .type = VAEncPackedHeaderRawData, .bit_length = (uint32_t)e->header_len * 8, .has_emulation_bytes = 0 };
    VABufferID b[6];
    va_check(vaCreateBuffer(a->va, e->ctx, VAEncPictureParameterBufferType, sizeof pic, 1, &pic, &b[0]), "pic buf");
    va_check(vaCreateBuffer(a->va, e->ctx, VAQMatrixBufferType, sizeof q, 1, &q, &b[1]), "q buf");
    va_check(vaCreateBuffer(a->va, e->ctx, VAHuffmanTableBufferType, sizeof hu, 1, &hu, &b[2]), "huff buf");
    va_check(vaCreateBuffer(a->va, e->ctx, VAEncPackedHeaderParameterBufferType, sizeof ph, 1, &ph, &b[3]), "ph buf");
    va_check(vaCreateBuffer(a->va, e->ctx, VAEncPackedHeaderDataBufferType, e->header_len, 1, e->header, &b[4]), "phd buf");
    va_check(vaCreateBuffer(a->va, e->ctx, VAEncSliceParameterBufferType, sizeof sl, 1, &sl, &b[5]), "slice buf");
    va_check(vaBeginPicture(a->va, e->ctx, e->nv12), "enc begin");
    va_check(vaRenderPicture(a->va, e->ctx, b, 6), "enc render");
    va_check(vaEndPicture(a->va, e->ctx), "enc end");
    va_check(vaSyncSurface(a->va, e->nv12), "enc sync");
    for (int i = 0; i < 6; i++) vaDestroyBuffer(a->va, b[i]);
    VACodedBufferSegment *seg; size_t total = 0;
    va_check(vaMapBuffer(a->va, e->coded, (void **)&seg), "map coded");
    for (VACodedBufferSegment *s = seg; s; s = s->next) total += s->size;
    /* Some drivers emit only the scan; prepend our header and ensure EOI. */
    int has_header = seg && seg->size >= 2 && ((uint8_t *)seg->buf)[0] == 0xFF && ((uint8_t *)seg->buf)[1] == 0xD8;
    uint8_t *out = malloc(total + e->header_len + 2); size_t n = 0;
    if (!has_header) { memcpy(out, e->header, e->header_len); n = e->header_len; }
    for (VACodedBufferSegment *s = seg; s; s = s->next) { memcpy(out + n, s->buf, s->size); n += s->size; }
    vaUnmapBuffer(a->va, e->coded);
    if (n < 2 || out[n - 2] != 0xFF || out[n - 1] != 0xD9) { out[n++] = 0xFF; out[n++] = 0xD9; }
    *out_len = n; return out;
}

/* ---------------- Wayland plumbing ---------------- */

static void output_name(void *d, struct wl_output *o, const char *name) { (void)o; snprintf(((struct output_info *)d)->name, 128, "%s", name); }
static void output_geometry(void*d,struct wl_output*o,int32_t x,int32_t y,int32_t pw,int32_t ph,int32_t s,const char*m,const char*md,int32_t t){(void)d;(void)o;(void)x;(void)y;(void)pw;(void)ph;(void)s;(void)m;(void)md;(void)t;}
static void output_mode(void*d,struct wl_output*o,uint32_t f,int32_t w,int32_t h,int32_t r){(void)d;(void)o;(void)f;(void)w;(void)h;(void)r;}
static void output_done(void*d,struct wl_output*o){(void)d;(void)o;}
static void output_scale(void*d,struct wl_output*o,int32_t s){(void)d;(void)o;(void)s;}
static void output_description(void*d,struct wl_output*o,const char*s){(void)d;(void)o;(void)s;}
static const struct wl_output_listener output_listener = { output_geometry, output_mode, output_done, output_scale, output_name, output_description };

static void registry_global(void *data, struct wl_registry *r, uint32_t name, const char *iface, uint32_t version) {
    struct app *a = data;
    if (!strcmp(iface, wl_shm_interface.name)) a->shm = wl_registry_bind(r, name, &wl_shm_interface, 1);
    else if (!strcmp(iface, wl_seat_interface.name) && !a->seat) a->seat = wl_registry_bind(r, name, &wl_seat_interface, version < 5 ? version : 5);
    else if (!strcmp(iface, zwp_linux_dmabuf_v1_interface.name) && version >= 3) a->dmabuf = wl_registry_bind(r, name, &zwp_linux_dmabuf_v1_interface, 3);
    else if (!strcmp(iface, ext_output_image_capture_source_manager_v1_interface.name)) a->source_manager = wl_registry_bind(r, name, &ext_output_image_capture_source_manager_v1_interface, 1);
    else if (!strcmp(iface, ext_image_copy_capture_manager_v1_interface.name)) a->capture_manager = wl_registry_bind(r, name, &ext_image_copy_capture_manager_v1_interface, 1);
    else if (!strcmp(iface, wl_output_interface.name) && a->output_count < 32) {
        struct output_info *info = &a->outputs[a->output_count++];
        info->object = wl_registry_bind(r, name, &wl_output_interface, version < 4 ? version : 4);
        wl_output_add_listener(info->object, &output_listener, info);
    }
}
static void registry_remove(void *d, struct wl_registry *r, uint32_t n) { (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = { registry_global, registry_remove };

/* Main session constraints */
static void s_size(void *d, struct ext_image_copy_capture_session_v1 *s, uint32_t w, uint32_t h) { (void)s; struct app *a = d; a->width = w; a->height = h; }
static void s_shm(void *d, struct ext_image_copy_capture_session_v1 *s, uint32_t f) { (void)d; (void)s; (void)f; }
static void s_dev(void *d, struct ext_image_copy_capture_session_v1 *s, struct wl_array *dev) {
    (void)s; struct app *a = d;
    if (dev->size == sizeof(dev_t)) { memcpy(&a->dmabuf_dev, dev->data, sizeof(dev_t)); a->have_dmabuf_dev = 1; }
}
static void s_fmt(void *d, struct ext_image_copy_capture_session_v1 *s, uint32_t f, struct wl_array *mods) {
    (void)s; struct app *a = d; uint64_t *m; uint32_t *count; uint64_t *dst;
    if (f == DRM_FORMAT_XRGB8888) { count = &a->fmt_xrgb_count; dst = a->mods_xrgb; }
    else if (f == DRM_FORMAT_ARGB8888) { count = &a->fmt_argb_count; dst = a->mods_argb; }
    else return;
    *count = 0;
    wl_array_for_each(m, mods) if (*count < 64) dst[(*count)++] = *m;
    if (a->probe) {
        fprintf(stderr, "{\"event\":\"dmabuf-format\",\"format\":\"%.4s\",\"modifiers\":[", (char *)&f);
        for (uint32_t i = 0; i < *count; i++) fprintf(stderr, "%s\"0x%016llx\"", i ? "," : "", (unsigned long long)dst[i]);
        fprintf(stderr, "]}\n");
    }
}
static void s_done(void *d, struct ext_image_copy_capture_session_v1 *s) { (void)s; ((struct app *)d)->constraints_done = 1; }
static void s_stopped(void *d, struct ext_image_copy_capture_session_v1 *s) { (void)s; ((struct app *)d)->stopped = 1; }
static const struct ext_image_copy_capture_session_v1_listener session_listener = { s_size, s_shm, s_dev, s_fmt, s_done, s_stopped };

/* Main frame events */
static void f_transform(void *d, struct ext_image_copy_capture_frame_v1 *f, uint32_t t) { (void)d; (void)f; (void)t; }
static void f_damage(void *d, struct ext_image_copy_capture_frame_v1 *f, int32_t x, int32_t y, int32_t w, int32_t h) {
    (void)f; struct frame_state *fs = d;
    if (fs->damage_count < MAX_DAMAGE) fs->damage[fs->damage_count++] = (struct rect){ x, y, w, h }; else fs->damage_overflow = 1;
}
static void f_time(void *d, struct ext_image_copy_capture_frame_v1 *f, uint32_t hi, uint32_t lo, uint32_t ns) { (void)f; ((struct frame_state *)d)->presentation_ns = ((((uint64_t)hi << 32) | lo) * 1000000000ULL) + ns; }
static void f_ready(void *d, struct ext_image_copy_capture_frame_v1 *f) { (void)f; struct frame_state *fs = d; fs->ready_ns = now_ns(); fs->done = 1; }
static void f_failed(void *d, struct ext_image_copy_capture_frame_v1 *f, uint32_t reason) { (void)f; struct frame_state *fs = d; fs->failed = 1; fs->fail_reason = reason; fs->done = 1; }
static const struct ext_image_copy_capture_frame_v1_listener frame_listener = { f_transform, f_damage, f_time, f_ready, f_failed };

/* ---------------- Cursor session ---------------- */

static void c_size(void *d, struct ext_image_copy_capture_session_v1 *s, uint32_t w, uint32_t h) { (void)s; struct cursor *c = d; c->pending_w = w; c->pending_h = h; }
static void c_shm(void *d, struct ext_image_copy_capture_session_v1 *s, uint32_t f) { (void)s; if (f == WL_SHM_FORMAT_ARGB8888) ((struct cursor *)d)->shm_argb = 1; }
static void c_dev(void *d, struct ext_image_copy_capture_session_v1 *s, struct wl_array *x) { (void)d; (void)s; (void)x; }
static void c_fmt(void *d, struct ext_image_copy_capture_session_v1 *s, uint32_t f, struct wl_array *x) { (void)d; (void)s; (void)f; (void)x; }
static void c_done(void *d, struct ext_image_copy_capture_session_v1 *s) { (void)s; ((struct cursor *)d)->constraints_done = 1; }
static void c_stopped(void *d, struct ext_image_copy_capture_session_v1 *s) { (void)s; (void)d; }
static const struct ext_image_copy_capture_session_v1_listener cursor_capture_listener = { c_size, c_shm, c_dev, c_fmt, c_done, c_stopped };

static void cf_transform(void *d, struct ext_image_copy_capture_frame_v1 *f, uint32_t t) { (void)d; (void)f; (void)t; }
static void cf_damage(void *d, struct ext_image_copy_capture_frame_v1 *f, int32_t x, int32_t y, int32_t w, int32_t h) { (void)d; (void)f; (void)x; (void)y; (void)w; (void)h; }
static void cf_time(void *d, struct ext_image_copy_capture_frame_v1 *f, uint32_t a, uint32_t b, uint32_t c) { (void)d; (void)f; (void)a; (void)b; (void)c; }
static void cf_ready(void *d, struct ext_image_copy_capture_frame_v1 *f) { (void)f; ((struct cursor *)d)->frame_done = 1; }
static void cf_failed(void *d, struct ext_image_copy_capture_frame_v1 *f, uint32_t r) { (void)f; (void)r; struct cursor *c = d; c->frame_failed = 1; c->frame_done = 1; }
static const struct ext_image_copy_capture_frame_v1_listener cursor_frame_listener = { cf_transform, cf_damage, cf_time, cf_ready, cf_failed };

static void cs_enter(void *d, struct ext_image_copy_capture_cursor_session_v1 *s) { (void)s; struct cursor *c = d; c->inside = 1; c->pos_dirty = 1; c->leave_dirty = 0; }
static void cs_leave(void *d, struct ext_image_copy_capture_cursor_session_v1 *s) { (void)s; struct cursor *c = d; c->inside = 0; c->leave_dirty = 1; c->pos_dirty = 0; }
static void cs_position(void *d, struct ext_image_copy_capture_cursor_session_v1 *s, int32_t x, int32_t y) { (void)s; struct cursor *c = d; c->x = x; c->y = y; c->pos_dirty = 1; }
static void cs_hotspot(void *d, struct ext_image_copy_capture_cursor_session_v1 *s, int32_t x, int32_t y) { (void)s; struct cursor *c = d; c->pending_hot_x = x; c->pending_hot_y = y; }
static const struct ext_image_copy_capture_cursor_session_v1_listener cursor_session_listener = { cs_enter, cs_leave, cs_position, cs_hotspot };

static int shm_buffer(struct app *a, uint32_t w, uint32_t h, struct wl_buffer **buffer, void **pixels, size_t *size) {
    *size = (size_t)w * h * 4;
    int fd = memfd_create("jua254-cursor", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, (off_t)*size) < 0) return -1;
    *pixels = mmap(NULL, *size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (*pixels == MAP_FAILED) { close(fd); return -1; }
    struct wl_shm_pool *pool = wl_shm_create_pool(a->shm, fd, (int)*size);
    *buffer = wl_shm_pool_create_buffer(pool, 0, w, h, w * 4, WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool); close(fd);
    return 0;
}

static void cursor_request(struct app *a) {
    struct cursor *c = &a->cursor;
    if (c->pending_w != c->width || c->pending_h != c->height || !c->buffer) {
        if (c->buffer) { wl_buffer_destroy(c->buffer); munmap(c->pixels, c->size); c->buffer = NULL; }
        if (!c->pending_w || !c->pending_h) return;
        if (shm_buffer(a, c->pending_w, c->pending_h, &c->buffer, &c->pixels, &c->size) < 0) die("cursor shm");
        c->width = c->pending_w; c->height = c->pending_h;
    }
    c->frame_done = c->frame_failed = 0;
    c->frame = ext_image_copy_capture_session_v1_create_frame(c->capture);
    ext_image_copy_capture_frame_v1_add_listener(c->frame, &cursor_frame_listener, c);
    ext_image_copy_capture_frame_v1_attach_buffer(c->frame, c->buffer);
    ext_image_copy_capture_frame_v1_damage_buffer(c->frame, 0, 0, INT32_MAX, INT32_MAX);
    ext_image_copy_capture_frame_v1_capture(c->frame);
}

static void cursor_service(struct app *a) {
    struct cursor *c = &a->cursor;
    if (!a->hw_cursor || !c->capture) return;
    if (c->frame && c->frame_done) {
        ext_image_copy_capture_frame_v1_destroy(c->frame); c->frame = NULL;
        if (!c->frame_failed) {
            c->hot_x = c->pending_hot_x; c->hot_y = c->pending_hot_y;
            emit(a, REC_CURSOR_SHAPE, c->hot_x, c->hot_y, c->width, c->height, c->pixels, (uint32_t)c->size, 0, now_ns());
            c->pos_dirty = c->inside;
        }
    }
    if (!c->frame && c->constraints_done) cursor_request(a);
    if (c->leave_dirty) { emit(a, REC_CURSOR_LEAVE, 0, 0, 0, 0, NULL, 0, 0, now_ns()); c->leave_dirty = 0; }
    if (c->pos_dirty && c->inside) { emit(a, REC_CURSOR_POS, c->x, c->y, 0, 0, NULL, 0, 0, now_ns()); c->pos_dirty = 0; }
}

/* ---------------- Capture buffers ---------------- */

static int modifier_allowed(uint64_t m) {
    /* Formats VA import handles on Intel; skip compression (CCS) modifiers. */
    return m == DRM_FORMAT_MOD_LINEAR || m == I915_FORMAT_MOD_X_TILED || m == I915_FORMAT_MOD_Y_TILED;
}

static void alloc_slots(struct app *a, struct gbm_device *gbm, uint32_t *format_out) {
    /* Hyprland 0.56 leaves XRGB8888 dmabuf targets empty; ARGB8888 is copied correctly. */
    int use_xrgb = !a->fmt_argb_count || getenv("JUA254_XRGB");
    uint32_t format = use_xrgb ? DRM_FORMAT_XRGB8888 : DRM_FORMAT_ARGB8888;
    uint32_t count = use_xrgb ? a->fmt_xrgb_count : a->fmt_argb_count;
    uint64_t *offered = use_xrgb ? a->mods_xrgb : a->mods_argb, mods[64]; uint32_t n = 0;
    if (!count) die("compositor offered no XRGB/ARGB dmabuf format");
    const char *force = getenv("JUA254_MODIFIER");
    for (uint32_t i = 0; i < count; i++)
        if (force ? offered[i] == strtoull(force, NULL, 0) : modifier_allowed(offered[i])) mods[n++] = offered[i];
    if (!n) die("no VA-importable modifier offered");
    for (int i = 0; i < SLOTS; i++) {
        struct slot *s = &a->slots[i];
        s->bo = gbm_bo_create_with_modifiers2(gbm, a->width, a->height, format, mods, n, GBM_BO_USE_RENDERING);
        if (!s->bo) die("gbm_bo_create_with_modifiers2");
        if (gbm_bo_get_plane_count(s->bo) != 1) die("multi-plane capture buffer");
        s->fd = gbm_bo_get_fd(s->bo); s->offset = gbm_bo_get_offset(s->bo, 0); s->stride = gbm_bo_get_stride_for_plane(s->bo, 0);
        s->modifier = gbm_bo_get_modifier(s->bo);
        struct zwp_linux_buffer_params_v1 *p = zwp_linux_dmabuf_v1_create_params(a->dmabuf);
        zwp_linux_buffer_params_v1_add(p, s->fd, 0, s->offset, s->stride, s->modifier >> 32, s->modifier & 0xffffffff);
        s->buffer = zwp_linux_buffer_params_v1_create_immed(p, a->width, a->height, format, 0);
        zwp_linux_buffer_params_v1_destroy(p);
        s->surface = import_dmabuf(a, s, format);
    }
    *format_out = format;
}

static void request_frame(struct app *a, int slot) {
    struct frame_state *fs = &a->inflight;
    memset(fs, 0, sizeof *fs); fs->slot = slot; fs->requested_ns = now_ns();
    fs->frame = ext_image_copy_capture_session_v1_create_frame(a->session);
    ext_image_copy_capture_frame_v1_add_listener(fs->frame, &frame_listener, fs);
    ext_image_copy_capture_frame_v1_attach_buffer(fs->frame, a->slots[slot].buffer);
    ext_image_copy_capture_frame_v1_damage_buffer(fs->frame, 0, 0, INT32_MAX, INT32_MAX);
    ext_image_copy_capture_frame_v1_capture(fs->frame);
    a->have_inflight = 1;
}

/* ---------------- Frame processing ---------------- */

static void process(struct app *a, struct frame_state *fs) {
    struct slot *s = &a->slots[fs->slot]; uint64_t t0 = now_ns();
    int cols = (a->width + a->tile_w - 1) / a->tile_w, rows = (a->height + a->tile_h - 1) / a->tile_h;
    static uint8_t mark[MAX_TILES]; int marked = 0, full = a->force_full || fs->damage_overflow || !a->damage_tiles;
    long damage_area = 0;
    if (!full) {
        if (cols * rows > MAX_TILES) full = 1;
        memset(mark, 0, sizeof mark);
        for (int i = 0; i < fs->damage_count && !full; i++) {
            struct rect r = fs->damage[i];
            int x0 = r.x < 0 ? 0 : r.x, y0 = r.y < 0 ? 0 : r.y;
            int x1 = r.x + r.w > (int)a->width ? (int)a->width : r.x + r.w, y1 = r.y + r.h > (int)a->height ? (int)a->height : r.y + r.h;
            if (x1 <= x0 || y1 <= y0) continue;
            damage_area += (long)(x1 - x0) * (y1 - y0);
            for (int ty = y0 / a->tile_h; ty <= (y1 - 1) / a->tile_h; ty++)
                for (int tx = x0 / a->tile_w; tx <= (x1 - 1) / a->tile_w; tx++)
                    if (!mark[ty * cols + tx]) { mark[ty * cols + tx] = 1; marked++; }
        }
        if (!full && (marked > a->max_tiles || (double)marked * a->tile_w * a->tile_h > a->full_threshold * a->width * a->height)) full = 1;
    }
    a->seq++; a->frames++;
    double vpp_ms = 0, enc_ms = 0; size_t bytes = 0;
    if (full) {
        uint64_t t = now_ns(); vpp(a, s->surface, &a->full, NULL); vpp_ms += ms_since(t);
    t = now_ns(); size_t len; uint8_t *jpeg = encode(a, &a->full, &len); enc_ms += ms_since(t);
        emit(a, REC_FULL, 0, 0, a->width, a->height, jpeg, (uint32_t)len, fs->requested_ns, fs->ready_ns);
        bytes = len; free(jpeg); a->full_sent++; a->force_full = 0; marked = 0;
    } else {
        for (int ty = 0; ty < rows; ty++) for (int tx = 0; tx < cols; tx++) {
            if (!mark[ty * cols + tx]) continue;
            /* Clamp edge tiles inside the frame so every tile has the encoder's size. */
            int x = tx * a->tile_w, y = ty * a->tile_h;
            if (x + a->tile_w > (int)a->width) x = a->width - a->tile_w;
            if (y + a->tile_h > (int)a->height) y = a->height - a->tile_h;
            VARectangle region = { (int16_t)x, (int16_t)y, (uint16_t)a->tile_w, (uint16_t)a->tile_h };
            uint64_t t = now_ns(); vpp(a, s->surface, &a->tile, &region); vpp_ms += ms_since(t);
            t = now_ns(); size_t len; uint8_t *jpeg = encode(a, &a->tile, &len); enc_ms += ms_since(t);
            emit(a, REC_TILE, x, y, a->tile_w, a->tile_h, jpeg, (uint32_t)len, fs->requested_ns, fs->ready_ns);
            bytes += len; free(jpeg); a->tiles_sent++;
        }
    }
    emit(a, REC_FRAME_END, full ? 0 : marked, full, 0, 0, NULL, 0, fs->requested_ns, fs->ready_ns);
    fprintf(stderr, "{\"event\":\"frame\",\"seq\":%u,\"full\":%s,\"tiles\":%d,\"damage_rects\":%d,\"damage_area\":%ld,"
            "\"requested_ns\":%llu,\"presentation_ns\":%llu,\"ready_wait_ms\":%.3f,\"vpp_ms\":%.3f,\"encode_ms\":%.3f,\"process_ms\":%.3f,\"bytes\":%zu}\n",
            a->seq, full ? "true" : "false", marked, fs->damage_count, damage_area,
            (unsigned long long)fs->requested_ns, (unsigned long long)fs->presentation_ns,
            (fs->ready_ns - fs->requested_ns) / 1e6, vpp_ms, enc_ms, ms_since(t0), bytes);
}

static void read_commands(struct app *a) {
    char buf[64]; ssize_t n = read(STDIN_FILENO, buf, sizeof buf);
    if (n == 0) exit(0);
    for (ssize_t i = 0; i < n; i++) if (buf[i] == 'F') a->force_full = 1;
}

static int dispatch_until(struct app *a, int64_t deadline_ns) {
    int fd = wl_display_get_fd(a->display);
    while (1) {
        if (wl_display_dispatch_pending(a->display) < 0) return -1;
        if (wl_display_flush(a->display) < 0 && errno != EAGAIN) return -1;
        cursor_service(a);
        if ((a->have_inflight && a->inflight.done) || a->stopped) return 0;
        int64_t left = (deadline_ns - (int64_t)now_ns()) / 1000000;
        if (left <= 0) return 1;
        struct pollfd p[2] = { { .fd = fd, .events = POLLIN }, { .fd = STDIN_FILENO, .events = POLLIN } };
        if (wl_display_prepare_read(a->display) != 0) continue;
        int rc = poll(p, 2, (int)left);
        if (rc < 0) { wl_display_cancel_read(a->display); if (errno == EINTR) continue; return -1; }
        if (p[0].revents & POLLIN) { if (wl_display_read_events(a->display) < 0) return -1; }
        else wl_display_cancel_read(a->display);
        if (p[1].revents & (POLLIN | POLLHUP)) read_commands(a);
    }
}

/* Offline check of VPP + encode: BGRA file in, full JPEG and one tile JPEG out. */
static int selftest(struct app *a, const char *path, int w, int h, int tx, int ty) {
    FILE *f = fopen(path, "rb"); if (!f) die("selftest input");
    size_t size = (size_t)w * h * 4; uint8_t *pixels = malloc(size);
    if (fread(pixels, 1, size, f) != size) die("selftest short input");
    fclose(f);
    VASurfaceAttrib attr = { .type = VASurfaceAttribPixelFormat, .flags = VA_SURFACE_ATTRIB_SETTABLE, .value = { .type = VAGenericValueTypeInteger, .value.i = VA_FOURCC_BGRA } };
    VASurfaceID src; VAImage img; void *map;
    va_check(vaCreateSurfaces(a->va, VA_RT_FORMAT_RGB32, w, h, &src, 1, &attr, 1), "selftest surface");
    va_check(vaDeriveImage(a->va, src, &img), "selftest derive");
    va_check(vaMapBuffer(a->va, img.buf, &map), "selftest map");
    for (int y = 0; y < h; y++) memcpy((uint8_t *)map + img.offsets[0] + (size_t)y * img.pitches[0], pixels + (size_t)y * w * 4, (size_t)w * 4);
    vaUnmapBuffer(a->va, img.buf); vaDestroyImage(a->va, img.image_id);
    size_t len; uint8_t *jpeg;
    vpp(a, src, &a->full, NULL); jpeg = encode(a, &a->full, &len); emit(a, REC_FULL, 0, 0, w, h, jpeg, (uint32_t)len, 0, 0); free(jpeg);
    VARectangle r = { (int16_t)tx, (int16_t)ty, (uint16_t)a->tile_w, (uint16_t)a->tile_h };
    vpp(a, src, &a->tile, &r); jpeg = encode(a, &a->tile, &len); emit(a, REC_TILE, tx, ty, a->tile_w, a->tile_h, jpeg, (uint32_t)len, 0, 0); free(jpeg);
    return 0;
}

static void usage(void) {
    fprintf(stderr, "usage: jua254-gpu-capture OUTPUT [--quality N] [--hw-cursor] [--damage-tiles] [--tile WxH] [--max-tiles N]\n"
                    "       [--full-threshold F] [--limited-range] [--frame-timeout-ms N] [--render-node PATH] [--probe]\n");
    exit(2);
}

int main(int argc, char **argv) {
    static struct app a; const char *wanted = NULL, *render_node = NULL, *selftest_path = NULL; int selftest_x = 0, selftest_y = 0;
    a.quality = 80; a.full_range = 1; a.tile_w = 32; a.tile_h = 1080; a.max_tiles = 24; a.full_threshold = 0.4; a.frame_timeout_ms = 500; a.force_full = 1;
    for (int i = 1; i < argc; i++) {
        const char *o = argv[i];
        if (!strcmp(o, "--quality") && i + 1 < argc) a.quality = atoi(argv[++i]);
        else if (!strcmp(o, "--hw-cursor")) a.hw_cursor = 1;
        else if (!strcmp(o, "--damage-tiles")) a.damage_tiles = 1;
        else if (!strcmp(o, "--tile") && i + 1 < argc) { if (sscanf(argv[++i], "%dx%d", &a.tile_w, &a.tile_h) != 2) usage(); }
        else if (!strcmp(o, "--max-tiles") && i + 1 < argc) a.max_tiles = atoi(argv[++i]);
        else if (!strcmp(o, "--full-threshold") && i + 1 < argc) a.full_threshold = atof(argv[++i]);
        else if (!strcmp(o, "--limited-range")) a.full_range = 0;
        else if (!strcmp(o, "--frame-timeout-ms") && i + 1 < argc) a.frame_timeout_ms = atoi(argv[++i]);
        else if (!strcmp(o, "--render-node") && i + 1 < argc) render_node = argv[++i];
        else if (!strcmp(o, "--probe")) a.probe = 1;
        else if (!strcmp(o, "--selftest") && i + 1 < argc) selftest_path = argv[++i];
        else if (!strcmp(o, "--selftest-tile-at") && i + 1 < argc) { if (sscanf(argv[++i], "%d,%d", &selftest_x, &selftest_y) != 2) usage(); }
        else if (o[0] == '-' || wanted) usage();
        else wanted = o;
    }
    if (selftest_path) {
        /* selftest: OUTPUT is WxH of the input file; no Wayland involved. */
        if (!wanted || sscanf(wanted, "%ux%u", &a.width, &a.height) != 2) usage();
        int fd = open(render_node ? render_node : "/dev/dri/renderD128", O_RDWR | O_CLOEXEC); if (fd < 0) die("open render node");
        a.va = vaGetDisplayDRM(fd); int ma, mi; va_check(vaInitialize(a.va, &ma, &mi), "vaInitialize");
        va_check(vaCreateConfig(a.va, VAProfileNone, VAEntrypointVideoProc, NULL, 0, &a.vpp_config), "vpp config");
        va_check(vaCreateContext(a.va, a.vpp_config, a.width, a.height, 0, NULL, 0, &a.vpp_ctx), "vpp context");
        encoder_init(&a, &a.full, a.width, a.height); encoder_init(&a, &a.tile, a.tile_w, a.tile_h);
        return selftest(&a, selftest_path, a.width, a.height, selftest_x, selftest_y);
    }
    if (!wanted || a.quality < 1 || a.quality > 100 || a.tile_w < 16 || a.tile_h < 16 || a.tile_w % 16 || (a.tile_h % 16 && a.tile_h != 1080)) usage();
    if (a.frame_timeout_ms < 50) a.frame_timeout_ms = 50;
    fcntl(STDIN_FILENO, F_SETFL, fcntl(STDIN_FILENO, F_GETFL) | O_NONBLOCK);

    a.display = wl_display_connect(NULL); if (!a.display) die("wl_display_connect");
    struct wl_registry *reg = wl_display_get_registry(a.display);
    wl_registry_add_listener(reg, &registry_listener, &a);
    wl_display_roundtrip(a.display); wl_display_roundtrip(a.display);
    if (!a.source_manager || !a.capture_manager || !a.dmabuf || !a.shm) die("required globals (ext-image-copy, linux-dmabuf v3, wl_shm) missing");
    struct wl_output *output = NULL;
    for (int i = 0; i < a.output_count; i++) if (!strcmp(a.outputs[i].name, wanted)) output = a.outputs[i].object;
    if (!output) { fprintf(stderr, "{\"event\":\"fatal\",\"error\":\"output %s not found; refusing fallback\"}\n", wanted); return 2; }

    struct ext_image_capture_source_v1 *source = ext_output_image_capture_source_manager_v1_create_source(a.source_manager, output);
    a.session = ext_image_copy_capture_manager_v1_create_session(a.capture_manager, source, a.hw_cursor ? 0 : EXT_IMAGE_COPY_CAPTURE_MANAGER_V1_OPTIONS_PAINT_CURSORS);
    ext_image_copy_capture_session_v1_add_listener(a.session, &session_listener, &a);
    if (a.hw_cursor) {
        if (!a.seat) die("no wl_seat for cursor session");
        a.pointer = wl_seat_get_pointer(a.seat);
        a.cursor.session = ext_image_copy_capture_manager_v1_create_pointer_cursor_session(a.capture_manager, source, a.pointer);
        ext_image_copy_capture_cursor_session_v1_add_listener(a.cursor.session, &cursor_session_listener, &a.cursor);
        a.cursor.capture = ext_image_copy_capture_cursor_session_v1_get_capture_session(a.cursor.session);
        ext_image_copy_capture_session_v1_add_listener(a.cursor.capture, &cursor_capture_listener, &a.cursor);
    }
    ext_image_capture_source_v1_destroy(source);
    while (!a.constraints_done && !a.stopped && wl_display_dispatch(a.display) >= 0) {}
    if (a.stopped || !a.width || !a.height) die("capture constraints failed");

    char node[64] = {0};
    if (!render_node && a.have_dmabuf_dev) {
        drmDevicePtr dev;
        if (drmGetDeviceFromDevId(a.dmabuf_dev, 0, &dev) == 0) {
            if (dev->available_nodes & (1 << DRM_NODE_RENDER)) snprintf(node, sizeof node, "%s", dev->nodes[DRM_NODE_RENDER]);
            drmFreeDevice(&dev);
        }
    }
    if (!render_node) render_node = node[0] ? node : "/dev/dri/renderD128";
    fprintf(stderr, "{\"event\":\"constraints\",\"output\":\"%s\",\"width\":%u,\"height\":%u,\"dmabuf_dev\":\"%u:%u\",\"render_node\":\"%s\",\"xrgb_modifiers\":%u,\"argb_modifiers\":%u,\"hw_cursor\":%s}\n",
            wanted, a.width, a.height, major(a.dmabuf_dev), minor(a.dmabuf_dev), render_node, a.fmt_xrgb_count, a.fmt_argb_count, a.hw_cursor ? "true" : "false");
    if (a.probe) return 0;

    int drm_fd = open(render_node, O_RDWR | O_CLOEXEC); if (drm_fd < 0) die("open render node");
    struct gbm_device *gbm = gbm_create_device(drm_fd); if (!gbm) die("gbm_create_device");
    a.va = vaGetDisplayDRM(drm_fd); int major_v, minor_v;
    va_check(vaInitialize(a.va, &major_v, &minor_v), "vaInitialize");
    va_check(vaCreateConfig(a.va, VAProfileNone, VAEntrypointVideoProc, NULL, 0, &a.vpp_config), "vpp config");
    va_check(vaCreateContext(a.va, a.vpp_config, a.width, a.height, 0, NULL, 0, &a.vpp_ctx), "vpp context");
    uint32_t format; alloc_slots(&a, gbm, &format);
    encoder_init(&a, &a.full, a.width, a.height);
    if (a.damage_tiles) {
        if (a.tile_w > (int)a.width) a.tile_w = a.width;
        if (a.tile_h > (int)a.height) a.tile_h = a.height;
        encoder_init(&a, &a.tile, a.tile_w, a.tile_h);
    }
    fprintf(stderr, "{\"event\":\"ready\",\"backend\":\"ext-image-copy-dmabuf-vaapi\",\"format\":\"%.4s\",\"modifier\":\"0x%016llx\",\"stride\":%u,\"width\":%u,\"height\":%u,"
            "\"quality\":%d,\"full_range\":%s,\"damage_tiles\":%s,\"tile\":\"%dx%d\",\"vendor\":\"%s\"}\n",
            (char *)&format, (unsigned long long)a.slots[0].modifier, a.slots[0].stride, a.width, a.height, a.quality,
            a.full_range ? "true" : "false", a.damage_tiles ? "true" : "false", a.tile_w, a.tile_h, vaQueryVendorString(a.va));
    fflush(stderr);

    int slot = 0;
    request_frame(&a, slot);
    while (!a.stopped) {
        int rc = dispatch_until(&a, (int64_t)a.inflight.requested_ns + (int64_t)a.frame_timeout_ms * 1000000);
        if (rc < 0) die("wayland dispatch");
        if (a.stopped) break;
        if (rc == 1) {
            /* No damage within the timeout: keep waiting on the same request (static screen). */
            fprintf(stderr, "{\"event\":\"idle\",\"timeout_ms\":%d}\n", a.frame_timeout_ms);
            a.inflight.requested_ns = now_ns();
            continue;
        }
        struct frame_state done = a.inflight;
        ext_image_copy_capture_frame_v1_destroy(done.frame); a.have_inflight = 0;
        if (done.failed) {
            fprintf(stderr, "{\"event\":\"frame-failed\",\"reason\":%u}\n", done.fail_reason);
            if (done.fail_reason == EXT_IMAGE_COPY_CAPTURE_FRAME_V1_FAILURE_REASON_STOPPED) break;
            if (done.fail_reason == EXT_IMAGE_COPY_CAPTURE_FRAME_V1_FAILURE_REASON_BUFFER_CONSTRAINTS) die("buffer constraints changed");
            request_frame(&a, slot); continue;
        }
        /* Overlap: capture the next frame into the other slot while this one encodes. */
        slot = (slot + 1) % SLOTS;
        request_frame(&a, slot);
        wl_display_flush(a.display);
        process(&a, &done);
    }
    fprintf(stderr, "{\"event\":\"stopped\",\"frames\":%llu,\"full\":%llu,\"tiles\":%llu}\n", (unsigned long long)a.frames, (unsigned long long)a.full_sent, (unsigned long long)a.tiles_sent);
    return 0;
}
