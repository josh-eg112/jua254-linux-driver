#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>
#include <poll.h>
#include <wayland-client.h>
#include "ext-image-capture-source-v1-client-protocol.h"
#include "ext-image-copy-capture-v1-client-protocol.h"

struct output_info { struct wl_output *object; char name[128]; };
struct app {
    struct wl_display *display; struct wl_shm *shm; struct wl_output *output;
    struct ext_output_image_capture_source_manager_v1 *source_manager;
    struct ext_image_copy_capture_manager_v1 *capture_manager;
    struct ext_image_copy_capture_session_v1 *session;
    struct wl_buffer *buffer[2]; void *pixels[2]; size_t size; uint32_t width,height,stride;
    const char *wanted; char output_name[128]; struct output_info outputs[32]; int output_count;
    int constraints_done, frame_done, failed, stopped;
    struct timespec requested, ready, copy_done;
    unsigned long long presentation_ns; int presentation_seen;
    int frame_timeout_ms;
};

struct writer {
    struct app *a; pthread_mutex_t mutex; pthread_cond_t cond; pthread_t thread;
    int queue[2], head, tail, count, free[2], stop, error;
};

static double ms_between(struct timespec a, struct timespec b) {
    return (b.tv_sec-a.tv_sec)*1000.0+(b.tv_nsec-a.tv_nsec)/1000000.0;
}
static unsigned long long ns_value(struct timespec value){return(unsigned long long)value.tv_sec*1000000000ULL+(unsigned long long)value.tv_nsec;}
static int make_memfd(size_t size) {
    char name[]="/tmp/jua254-cap-XXXXXX"; int fd=mkstemp(name);
    if(fd<0) return -1;
    unlink(name);
    if(ftruncate(fd,(off_t)size)<0){close(fd);return -1;}
    return fd;
}
static void output_name(void *data, struct wl_output *o, const char *name) {
    (void)o; struct output_info *info=data; snprintf(info->name,sizeof(info->name),"%s",name);
}
static void output_geometry(void*d,struct wl_output*o,int32_t x,int32_t y,int32_t pw,int32_t ph,int32_t sub,const char*m,const char*model,int32_t t){(void)d;(void)o;(void)x;(void)y;(void)pw;(void)ph;(void)sub;(void)m;(void)model;(void)t;}
static void output_mode(void*d,struct wl_output*o,uint32_t f,int32_t w,int32_t h,int32_t r){(void)d;(void)o;(void)f;(void)w;(void)h;(void)r;}
static void output_done(void*d,struct wl_output*o){(void)d;(void)o;}
static void output_scale(void*d,struct wl_output*o,int32_t s){(void)d;(void)o;(void)s;}
static void output_description(void*d,struct wl_output*o,const char*s){(void)d;(void)o;(void)s;}
static const struct wl_output_listener output_listener={output_geometry,output_mode,output_done,output_scale,output_name,output_description};

static void registry_global(void *data,struct wl_registry*r,uint32_t name,const char*iface,uint32_t version){
    struct app*a=data;
    if(!strcmp(iface,wl_shm_interface.name)) a->shm=wl_registry_bind(r,name,&wl_shm_interface,1);
    else if(!strcmp(iface,ext_output_image_capture_source_manager_v1_interface.name)) a->source_manager=wl_registry_bind(r,name,&ext_output_image_capture_source_manager_v1_interface,1);
    else if(!strcmp(iface,ext_image_copy_capture_manager_v1_interface.name)) a->capture_manager=wl_registry_bind(r,name,&ext_image_copy_capture_manager_v1_interface,1);
    else if(!strcmp(iface,wl_output_interface.name) && a->output_count < 32){
        struct output_info *info=&a->outputs[a->output_count++];
        info->object=wl_registry_bind(r,name,&wl_output_interface,version<4?version:4);
        wl_output_add_listener(info->object,&output_listener,info);
    }
}
static void registry_remove(void*d,struct wl_registry*r,uint32_t n){(void)d;(void)r;(void)n;}
static const struct wl_registry_listener registry_listener={registry_global,registry_remove};

static void session_size(void*d,struct ext_image_copy_capture_session_v1*s,uint32_t w,uint32_t h){(void)s;struct app*a=d;a->width=w;a->height=h;}
static void session_shm(void*d,struct ext_image_copy_capture_session_v1*s,uint32_t f){(void)s;(void)f;(void)d;}
static void session_dev(void*d,struct ext_image_copy_capture_session_v1*s,struct wl_array*x){(void)d;(void)s;(void)x;}
static void session_dma(void*d,struct ext_image_copy_capture_session_v1*s,uint32_t f,struct wl_array*x){(void)d;(void)s;(void)f;(void)x;}
static void session_done(void*d,struct ext_image_copy_capture_session_v1*s){(void)s;((struct app*)d)->constraints_done=1;}
static void session_stopped(void*d,struct ext_image_copy_capture_session_v1*s){(void)s;((struct app*)d)->stopped=1;}
static const struct ext_image_copy_capture_session_v1_listener session_listener={session_size,session_shm,session_dev,session_dma,session_done,session_stopped};

static void frame_transform(void*d,struct ext_image_copy_capture_frame_v1*f,uint32_t t){(void)d;(void)f;(void)t;}
static void frame_damage(void*d,struct ext_image_copy_capture_frame_v1*f,int32_t x,int32_t y,int32_t w,int32_t h){(void)d;(void)f;(void)x;(void)y;(void)w;(void)h;}
static void frame_time(void*d,struct ext_image_copy_capture_frame_v1*f,uint32_t hi,uint32_t lo,uint32_t ns){(void)f;struct app*a=d;a->presentation_ns=((((unsigned long long)hi<<32)|lo)*1000000000ULL)+ns;a->presentation_seen=1;}
static void frame_ready(void*d,struct ext_image_copy_capture_frame_v1*f){(void)f;struct app*a=d;clock_gettime(CLOCK_MONOTONIC,&a->ready);a->frame_done=1;}
static void frame_failed(void*d,struct ext_image_copy_capture_frame_v1*f,uint32_t reason){(void)f;struct app*a=d;a->failed=1;a->frame_done=1;fprintf(stderr,"{\"event\":\"frame-failed\",\"reason\":%u}\n",reason);}
static const struct ext_image_copy_capture_frame_v1_listener frame_listener={frame_transform,frame_damage,frame_time,frame_ready,frame_failed};

static int write_all(const void *buf,size_t size){const uint8_t*p=buf;while(size){ssize_t n=write(STDOUT_FILENO,p,size);if(n<0){if(errno==EINTR)continue;return -1;}p+=n;size-=n;}return 0;}
static int dispatch_frame(struct app *a) {
    int fd=wl_display_get_fd(a->display); struct timespec now;
    clock_gettime(CLOCK_MONOTONIC,&now);
    double deadline=ms_between(a->requested,now)+a->frame_timeout_ms;
    while(!a->frame_done&&!a->stopped) {
        if(wl_display_dispatch_pending(a->display)<0) return -1;
        if(a->frame_done) break;
        if(wl_display_flush(a->display)<0 && errno!=EAGAIN) return -1;
        clock_gettime(CLOCK_MONOTONIC,&now);
        int left=(int)(deadline-ms_between(a->requested,now));
        if(left<=0) return 1;
        struct pollfd p={.fd=fd,.events=POLLIN}; int rc=poll(&p,1,left);
        if(rc<0){if(errno==EINTR)continue;return -1;}
        if(rc==0)return 1;
        if(wl_display_dispatch(a->display)<0)return -1;
    }
    return 0;
}
static void *writer_main(void *opaque){
    struct writer*w=opaque;
    for(;;){
        pthread_mutex_lock(&w->mutex);
        while(!w->count&&!w->stop)pthread_cond_wait(&w->cond,&w->mutex);
        if(!w->count&&w->stop){pthread_mutex_unlock(&w->mutex);break;}
        int idx=w->queue[w->head];w->head=(w->head+1)%2;w->count--;
        pthread_mutex_unlock(&w->mutex);
        if(write_all(w->a->pixels[idx],w->a->size)<0)w->error=1;
        pthread_mutex_lock(&w->mutex);w->free[idx]=1;pthread_cond_broadcast(&w->cond);pthread_mutex_unlock(&w->mutex);
        if(w->error)break;
    }
    return NULL;
}
int main(int argc,char**argv){
    struct app a={0}; int double_buffer=0; a.frame_timeout_ms=500; a.wanted=argc>1?argv[1]:"eDP-1";
    for(int i=2;i<argc;i++){if(!strcmp(argv[i],"--double-buffer"))double_buffer=1; else if(!strcmp(argv[i],"--frame-timeout-ms")&&i+1<argc)a.frame_timeout_ms=atoi(argv[++i]);}
    if(a.frame_timeout_ms<50)a.frame_timeout_ms=50;
    a.display=wl_display_connect(NULL); if(!a.display){perror("wl_display_connect");return 2;}
    struct wl_registry*r=wl_display_get_registry(a.display); wl_registry_add_listener(r,&registry_listener,&a); wl_display_roundtrip(a.display); wl_display_roundtrip(a.display);
    if(!a.shm||!a.output_count||!a.source_manager||!a.capture_manager){fprintf(stderr,"required ext-image-copy globals unavailable\n");return 2;}
    fprintf(stderr,"{\"event\":\"output-selection\",\"requested\":\"%s\",\"protocol\":\"ext-image-copy-v1\",\"outputs\":[",a.wanted);
    int selected=-1;
    for(int i=0;i<a.output_count;i++){
        if(i) fputc(',',stderr); fprintf(stderr,"\"%s\"",a.outputs[i].name[0]?a.outputs[i].name:"<unnamed>");
        if(!strcmp(a.outputs[i].name,a.wanted)) selected=i;
    }
    fprintf(stderr,"]}"); fputc('\n',stderr);
    if(selected<0){fprintf(stderr,"requested output %s was not enumerated; refusing fallback to another output\n",a.wanted);return 2;}
    a.output=a.outputs[selected].object; snprintf(a.output_name,sizeof(a.output_name),"%s",a.outputs[selected].name);
    fprintf(stderr,"{\"event\":\"output-selected\",\"requested\":\"%s\",\"selected\":\"%s\",\"protocol\":\"ext-image-copy-v1\"}\n",a.wanted,a.output_name);
    struct ext_image_capture_source_v1*source=ext_output_image_capture_source_manager_v1_create_source(a.source_manager,a.output);
    /* Include the compositor cursor in each captured output frame. */
    a.session=ext_image_copy_capture_manager_v1_create_session(a.capture_manager,source,EXT_IMAGE_COPY_CAPTURE_MANAGER_V1_OPTIONS_PAINT_CURSORS); ext_image_capture_source_v1_destroy(source);
    ext_image_copy_capture_session_v1_add_listener(a.session,&session_listener,&a);
    while(!a.constraints_done&&!a.stopped && wl_display_dispatch(a.display)>=0){}
    if(a.stopped||a.width==0||a.height==0){fprintf(stderr,"capture constraints failed\n");return 3;}
    a.stride=a.width*4; a.size=(size_t)a.stride*a.height; int buffers=double_buffer?2:1;
    for(int i=0;i<buffers;i++){int fd=make_memfd(a.size); if(fd<0){perror("capture tempfile");return 3;}
        a.pixels[i]=mmap(NULL,a.size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0); if(a.pixels[i]==MAP_FAILED){perror("mmap");return 3;}
        struct wl_shm_pool*pool=wl_shm_create_pool(a.shm,fd,(int)a.size); a.buffer[i]=wl_shm_pool_create_buffer(pool,0,(int)a.width,(int)a.height,(int)a.stride,WL_SHM_FORMAT_ARGB8888); wl_shm_pool_destroy(pool); close(fd);}
    struct writer w={.a=&a,.free={1,1}}; pthread_mutex_init(&w.mutex,NULL);pthread_cond_init(&w.cond,NULL);
    if(double_buffer)pthread_create(&w.thread,NULL,writer_main,&w);
    fprintf(stderr,"{\"event\":\"ready\",\"backend\":\"ext-image-copy-shm\",\"buffers\":%d,\"width\":%u,\"height\":%u,\"format\":\"bgra\"}\n",buffers,a.width,a.height); fflush(stderr);
    unsigned long seq=0; int idx=0;
    while(!a.stopped){
        if(double_buffer){pthread_mutex_lock(&w.mutex);while(!w.free[idx]&&!w.error)pthread_cond_wait(&w.cond,&w.mutex);w.free[idx]=0;pthread_mutex_unlock(&w.mutex);if(w.error)break;}
        a.frame_done=a.failed=a.presentation_seen=0; clock_gettime(CLOCK_MONOTONIC,&a.requested);
        struct ext_image_copy_capture_frame_v1*f=ext_image_copy_capture_session_v1_create_frame(a.session); ext_image_copy_capture_frame_v1_add_listener(f,&frame_listener,&a);
        ext_image_copy_capture_frame_v1_attach_buffer(f,a.buffer[idx]); ext_image_copy_capture_frame_v1_damage_buffer(f,0,0,INT32_MAX,INT32_MAX); ext_image_copy_capture_frame_v1_capture(f);
        int dispatch_rc=dispatch_frame(&a);
        ext_image_copy_capture_frame_v1_destroy(f);
        if(dispatch_rc==1){fprintf(stderr,"{\"event\":\"frame-timeout\",\"timeout_ms\":%d}\n",a.frame_timeout_ms);if(double_buffer){pthread_mutex_lock(&w.mutex);w.free[idx]=1;pthread_cond_signal(&w.cond);pthread_mutex_unlock(&w.mutex);}continue;}
        if(dispatch_rc<0||a.failed){if(double_buffer){pthread_mutex_lock(&w.mutex);w.free[idx]=1;pthread_cond_signal(&w.cond);pthread_mutex_unlock(&w.mutex);}if(dispatch_rc<0)break;continue;} if(a.stopped)break;
        double copy_ms=0;
        if(double_buffer){pthread_mutex_lock(&w.mutex);w.queue[w.tail]=idx;w.tail=(w.tail+1)%2;w.count++;pthread_cond_signal(&w.cond);pthread_mutex_unlock(&w.mutex);idx=(idx+1)%2;}
        else {if(write_all(a.pixels[idx],a.size)<0)break;clock_gettime(CLOCK_MONOTONIC,&a.copy_done);copy_ms=ms_between(a.ready,a.copy_done);}
        fprintf(stderr,"{\"event\":\"frame\",\"seq\":%lu,\"requested_ns\":%llu,\"presentation_ns\":%llu,\"presentation_seen\":%s,\"ready_wait_ms\":%.3f,\"copy_ms\":%.3f}\n",++seq,ns_value(a.requested),a.presentation_ns,a.presentation_seen?"true":"false",ms_between(a.requested,a.ready),copy_ms);
    }
    if(double_buffer){pthread_mutex_lock(&w.mutex);w.stop=1;pthread_cond_signal(&w.cond);pthread_mutex_unlock(&w.mutex);pthread_join(w.thread,NULL);}
    return 0;
}
