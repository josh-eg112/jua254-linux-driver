PROTOCOLS := /usr/share/wayland-protocols/staging
GEN := generated
CFLAGS += -O3 -Wall -Wextra -pthread $(shell pkg-config --cflags wayland-client)
LDLIBS += -pthread $(shell pkg-config --libs wayland-client)

all: jua254-extcopy-capture jua254-gpu-capture

$(GEN):
	mkdir -p $@

$(GEN)/ext-image-copy-capture-v1-client-protocol.h: $(PROTOCOLS)/ext-image-copy-capture/ext-image-copy-capture-v1.xml | $(GEN)
	wayland-scanner client-header $< $@

$(GEN)/ext-image-copy-capture-v1-protocol.c: $(PROTOCOLS)/ext-image-copy-capture/ext-image-copy-capture-v1.xml | $(GEN)
	wayland-scanner private-code $< $@

$(GEN)/ext-image-capture-source-v1-client-protocol.h: $(PROTOCOLS)/ext-image-capture-source/ext-image-capture-source-v1.xml | $(GEN)
	wayland-scanner client-header $< $@

$(GEN)/ext-image-capture-source-v1-protocol.c: $(PROTOCOLS)/ext-image-capture-source/ext-image-capture-source-v1.xml | $(GEN)
	wayland-scanner private-code $< $@

$(GEN)/ext-foreign-toplevel-list-v1-protocol.c: $(PROTOCOLS)/ext-foreign-toplevel-list/ext-foreign-toplevel-list-v1.xml | $(GEN)
	wayland-scanner private-code $< $@

jua254-extcopy-capture: extcopy_capture.c $(GEN)/ext-image-copy-capture-v1-client-protocol.h $(GEN)/ext-image-copy-capture-v1-protocol.c $(GEN)/ext-image-capture-source-v1-client-protocol.h $(GEN)/ext-image-capture-source-v1-protocol.c $(GEN)/ext-foreign-toplevel-list-v1-protocol.c
	$(CC) $(CFLAGS) -I$(GEN) $^ $(LDLIBS) -o $@

$(GEN)/linux-dmabuf-v1-client-protocol.h: /usr/share/wayland-protocols/stable/linux-dmabuf/linux-dmabuf-v1.xml | $(GEN)
	wayland-scanner client-header $< $@

$(GEN)/linux-dmabuf-v1-protocol.c: /usr/share/wayland-protocols/stable/linux-dmabuf/linux-dmabuf-v1.xml | $(GEN)
	wayland-scanner private-code $< $@

GPU_PKGS := wayland-client libva libva-drm gbm libdrm

jua254-gpu-capture: gpu_capture.c jpeg_tables.h $(GEN)/ext-image-copy-capture-v1-client-protocol.h $(GEN)/ext-image-copy-capture-v1-protocol.c $(GEN)/ext-image-capture-source-v1-client-protocol.h $(GEN)/ext-image-capture-source-v1-protocol.c $(GEN)/linux-dmabuf-v1-client-protocol.h $(GEN)/linux-dmabuf-v1-protocol.c $(GEN)/ext-foreign-toplevel-list-v1-protocol.c
	$(CC) $(CFLAGS) $(shell pkg-config --cflags $(GPU_PKGS)) -I$(GEN) $(filter %.c,$^) $(shell pkg-config --libs $(GPU_PKGS)) -o $@

clean:
	rm -f jua254-extcopy-capture jua254-gpu-capture
	rm -rf $(GEN)

.PHONY: all clean
