#include "mock.h"

extern int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size) {
  process_input_header(Data, Size);
  return 0;
}
