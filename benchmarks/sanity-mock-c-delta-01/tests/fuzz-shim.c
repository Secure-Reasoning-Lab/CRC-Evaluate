// minimal shim for LLVMFuzzerTestOneInput

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size);

uint8_t *read_file(const char *path, size_t *size) {
  FILE *file = fopen(path, "rb");
  if (!file) {
    fprintf(stderr, "Failed to open file: %s\n", path);
    return NULL;
  }
  fseek(file, 0, SEEK_END);
  *size = ftell(file);
  fseek(file, 0, SEEK_SET);
  uint8_t *data = (uint8_t *)malloc(*size);
  fread(data, 1, *size, file);
  fclose(file);
  return data;
}

int main(int argc, char **argv) {
  if (argc != 2) {
    fprintf(stderr, "Usage: %s <file>\n", argv[0]);
    return 1;
  }
  size_t size;
  uint8_t *data = read_file(argv[1], &size);
  if (!data) {
    fprintf(stderr, "Failed to read file: %s\n", argv[1]);
    return 1;
  }
  LLVMFuzzerTestOneInput(data, size);
  puts("Executed");
  free(data);
  return 0;
}
