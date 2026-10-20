// The app's JPEG encoder: libjpeg-turbo (static, from build/lib/libjpeg.a) behind a few calls for Swift.
// See turbo.c and Encoder.swift.
#include <stddef.h>
#include <stdint.h>

typedef struct turbo_encoder turbo_encoder;

/// An encoder writing into a buffer of `capacity` bytes it owns. Use from one thread at a time.
turbo_encoder *turbo_encoder_new(size_t capacity);
void turbo_encoder_free(turbo_encoder *encoder);

/// Result of turbo_encode when the JPEG would be larger than the encoder's capacity.
#define TURBO_TOO_LARGE ((size_t)-1)

/// Encode BGRA (the alpha byte ignored) as a baseline JFIF JPEG with 4:2:0 chroma, the standard
/// Huffman tables and a restart marker every `restart_interval` MCUs (0 for none). `quality` is
/// libjpeg's 1-100. Returns the JPEG's size (its bytes are at turbo_output until the next call),
/// TURBO_TOO_LARGE as soon as it is known not to fit, or 0 on any other error.
size_t turbo_encode(turbo_encoder *encoder, const uint8_t *bgra, size_t row_bytes, int width, int height,
                    int quality, int restart_interval);
const uint8_t *turbo_output(const turbo_encoder *encoder);
