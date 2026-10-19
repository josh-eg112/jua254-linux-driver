#include "turbo.h"
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <jpeglib.h>

struct turbo_encoder {
    struct jpeg_compress_struct c;
    struct jpeg_error_mgr error;
    struct jpeg_destination_mgr destination;
    jmp_buf jump;
    int too_large;
    uint8_t *buffer;
    size_t capacity;
};

// libjpeg's errors (and a full buffer) end the encode by jumping back into turbo_encode.
static void on_error(j_common_ptr c) {
    turbo_encoder *e = (turbo_encoder *)c->client_data;
    longjmp(e->jump, 1);
}
static void on_message(j_common_ptr c) { (void)c; }  // warnings: nothing the adapter cares about

// The output goes straight into the preallocated buffer. If it fills up the frame can't be sent
// anyway, so stop there rather than finish encoding it.
static void start_output(j_compress_ptr c) {
    turbo_encoder *e = (turbo_encoder *)c->client_data;
    e->destination.next_output_byte = e->buffer;
    e->destination.free_in_buffer = e->capacity;
}
static boolean buffer_full(j_compress_ptr c) {
    turbo_encoder *e = (turbo_encoder *)c->client_data;
    e->too_large = 1;
    longjmp(e->jump, 1);
}
static void end_output(j_compress_ptr c) { (void)c; }

turbo_encoder *turbo_encoder_new(size_t capacity) {
    turbo_encoder *e = calloc(1, sizeof *e);
    if (!e || !(e->buffer = malloc(capacity))) { free(e); return NULL; }
    e->capacity = capacity;
    e->c.err = jpeg_std_error(&e->error);
    e->error.error_exit = on_error;
    e->error.output_message = on_message;
    e->c.client_data = e;
    if (setjmp(e->jump)) { free(e->buffer); free(e); return NULL; }
    jpeg_create_compress(&e->c);
    e->destination.init_destination = start_output;
    e->destination.empty_output_buffer = buffer_full;
    e->destination.term_destination = end_output;
    e->c.dest = &e->destination;
    return e;
}

void turbo_encoder_free(turbo_encoder *e) {
    if (!e) return;
    jpeg_destroy_compress(&e->c);
    free(e->buffer);
    free(e);
}

const uint8_t *turbo_output(const turbo_encoder *e) { return e->buffer; }

size_t turbo_encode(turbo_encoder *e, const uint8_t *bgra, size_t row_bytes, int width, int height,
                    int quality, int restart_interval) {
    struct jpeg_compress_struct *c = &e->c;
    e->too_large = 0;
    if (setjmp(e->jump)) {
        jpeg_abort_compress(c);
        return e->too_large ? TURBO_TOO_LARGE : 0;
    }
    c->image_width = (JDIMENSION)width;
    c->image_height = (JDIMENSION)height;
    c->input_components = 4;
    c->in_color_space = JCS_EXT_BGRX;  // libjpeg-turbo converts and downsamples with its SIMD code
    jpeg_set_defaults(c);              // YCbCr, 4:2:0 (2x2, 1x1, 1x1), JFIF APP0, ISLOW DCT
    jpeg_set_quality(c, quality, TRUE);  // TRUE: quantisation tables limited to 8 bits, as baseline needs
    c->optimize_coding = FALSE;        // the standard Huffman tables, so one pass and SOF0
    c->restart_interval = (unsigned int)restart_interval;
    jpeg_start_compress(c, TRUE);
    JSAMPROW rows[16];
    while (c->next_scanline < c->image_height) {
        JDIMENSION n = 0;
        for (; n < 16 && c->next_scanline + n < c->image_height; n++)
            rows[n] = (JSAMPROW)(bgra + (size_t)(c->next_scanline + n) * row_bytes);
        jpeg_write_scanlines(c, rows, n);
    }
    jpeg_finish_compress(c);
    return e->capacity - e->destination.free_in_buffer;
}
