# Interface to run standardized CRS interface
## NOTE
The following interface are still not finalized.


## Building CRS docker image
It starts with `build_crs` command with following arguments:

- configuration files directory for a CRS `ensemble-c`: `infra/crs/example_configs/ensemble-c`
- project name: `json-c`

```sh
python3 infra/helper.py build_crs infra/crs/example_configs/ensemble-c json-c
```

## Running CRS for specific fuzzing harnesses
`run_crs` accepts the same arguments as `build_crs`, it additionally accept the
following arguments:

- fuzzing harness name: `json_array_fuzzer`

```sh
python3 infra/helper.py run_crs infra/crs/example_configs/ensemble-c json-c json_array_fuzzer
```

## How filesystem is mapped between host and docker container
On host, it is `build/out/<crs-name>/<project-name>/<harness-name>/{crashes,
corpus}`.
In the docker container, the directory will be mapped to `/out/<harness-name>/{crashes, corpus}`.
