---
name: vuln-yaml-generator
description: Generate vuln.yaml files from vulnerability analysis results. Use when creating or updating vuln.yaml for CRSBench CPVs based on vulnerability analysis markdown.
---

# Vulnerability YAML Generator

Generate a vuln.yaml file from vulnerability analysis results.

## vuln.yaml Format

Generate a valid YAML file with this structure:

```yaml
id: {cpv_id}

name: <Short, descriptive vulnerability name>

origin: <1-day or synthetic>

release_date: <MM/DD/YYYY>

references:  # List of URLs (required for 1-day, optional for synthetic)
- <URL to CVE/OSV/advisory>
- <URL to another reference if available>

cwes:
- CWE-XXX
- CWE-YYY

description: |
  <Clear, multi-line description of the vulnerability.
  Include what causes it, where it occurs, and potential impact.>

locations:
- type: <crash_site or root_cause>
  path_from_root: <relative/path/from/project/root.c>
  function_name: <vulnerable_function>
  startLine: <line_number>
  startColumn: <column_number>
  endLine: <line_number>
  endColumn: <column_number>
```

## Field Guidelines

### id
- Must be exactly the provided CPV ID (e.g., `cpv_0`)

### origin
- Valid values: `1-day` or `synthetic`
- `1-day`: **ONLY if CVE/OSV ID was found in patch files**
- `synthetic`: **DEFAULT** - use if no CVE ID in patches
- ⚠️ DO NOT guess based on web search results
- ⚠️ If analysis says "synthetic" or no CVE found → use `synthetic`

### release_date
- Format: `MM/DD/YYYY` (e.g., `02/16/2017`)
- For 1-day: Official CVE/advisory publication date
- For synthetic: Use `02/01/2026`

### references
- List of URLs to vulnerability details (required for 1-day, optional for synthetic)
- A single vulnerability may have multiple references (CVE + GHSA, etc.)
- Valid reference sources:
  - CVE: `https://nvd.nist.gov/vuln/detail/CVE-2017-6009`
  - OSV: `https://osv.dev/vulnerability/OSV-2021-1234`
  - GHSA: `https://github.com/advisories/GHSA-xxxx-xxxx-xxxx`
  - Bug tracker: `https://github.com/project/issues/XXX`

### name
- Short, descriptive name (max 80 characters)
- Examples:
  - "Heap buffer overflow in cr_buf_read"
  - "Use-after-free in HTTP chunk processing"
  - "NULL pointer dereference in JSON parser"
- DO NOT use generic names like "MOCK: cpv_0 vulnerability"
- **CRITICAL**: Avoid special YAML characters in unquoted values:
  - DO NOT use colons (`:`) in unquoted field values
  - If you must include special characters, wrap the entire value in quotes

### cwes
- List of relevant CWE numbers (1-3 most relevant)
- Common CWEs:
  - CWE-787: Out-of-bounds Write
  - CWE-125: Out-of-bounds Read
  - CWE-416: Use After Free
  - CWE-476: NULL Pointer Dereference
  - CWE-190: Integer Overflow
  - CWE-122: Heap-based Buffer Overflow
  - CWE-121: Stack-based Buffer Overflow

### description
- Clear explanation of the vulnerability
- Use `|` for multi-line strings
- Include: type, location, cause, and security impact

### locations
- At least one location (the primary vulnerable code location)
- **type** (REQUIRED):
  - `crash_site`: Where the crash/error occurs (from crash log stack trace)
  - `root_cause`: Where the underlying defect exists (from patch analysis)
  - If only one location and it's both, use `crash_site`
- **EXCLUDE these from locations:**
  - Harness files (`*_fuzzer.c`, `*_fuzzer.cc`, `LLVMFuzzerTestOneInput`)
  - ASan/sanitizer functions (`__asan_*`, `__msan_*`, `__ubsan_*`)
  - Standard library functions (`memcpy`, `malloc`, `free`, etc.)
  - Compiler runtime (`llvm-project/compiler-rt/*`)
- **path_from_root**: Relative path from project root (NOT absolute path)
- **function_name**: Exact function name from crash log
- **startLine/endLine**: Line range of vulnerable code
- **startColumn/endColumn**: Column range (use 0 if unknown)

## Output

Output ONLY the vuln.yaml content (no markdown fences, no explanation).

## Example Output

```yaml
id: cpv_0

name: Heap buffer overflow in cr_buf_read

origin: 1-day

release_date: 02/16/2017

references:
- https://nvd.nist.gov/vuln/detail/CVE-2017-6009

cwes:
- CWE-122
- CWE-787

description: |
  A heap buffer overflow vulnerability exists in the cr_buf_read function
  in lib/sendf.c. The function reads data into a buffer without properly
  checking the buffer size, allowing an attacker to write beyond the
  allocated memory. This can lead to arbitrary code execution or denial
  of service.

locations:
- type: crash_site
  path_from_root: lib/sendf.c
  function_name: cr_buf_read
  startLine: 1298
  startColumn: 5
  endLine: 1298
  endColumn: 45
- type: root_cause
  path_from_root: lib/sendf.c
  function_name: allocate_buffer
  startLine: 1250
  startColumn: 1
  endLine: 1255
  endColumn: 1
```

## Validation Checklist

Before outputting, verify:
- [ ] `id` matches the provided CPV ID
- [ ] `origin` is either `1-day` or `synthetic`
- [ ] `release_date` is in `MM/DD/YYYY` format
- [ ] `references` is a list of valid URLs (required for 1-day, optional for synthetic)
- [ ] `name` has no unquoted special characters
- [ ] `cwes` is a non-empty list
- [ ] `description` is non-empty and uses `|` for multiline
- [ ] `locations` is a non-empty list
- [ ] Each location has a valid `type` (crash_site or root_cause)
- [ ] Each location has `path_from_root` (relative path, not absolute)
- [ ] Each location has `function_name`
- [ ] Each location has line/column numbers
