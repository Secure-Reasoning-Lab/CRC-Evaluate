---
name: vuln-yaml-analyzer
description: Analyze vulnerabilities from crash logs, POV files, patches, and source code. Use when analyzing CPV vulnerabilities, preparing vuln.yaml generation, or investigating security issues in CRSBench benchmarks.
---

# Vulnerability Analyzer

Analyze a vulnerability from crash logs, POV files, patches, and source code to prepare for vuln.yaml generation.

## Analysis Strategy

### Step 1: Analyze Crash Log (CRITICAL - Start Here)

The crash log is the primary source of vulnerability information.

**CRITICAL**:
- **ONLY** read crash log files from the CPV's logs directory
- **DO NOT** search for crash logs from other CPVs

Find and read crash log files (e.g., `pov_0.log`, `crash.log`).

Extract key information:
- **Error type**: AddressSanitizer error (heap-buffer-overflow, use-after-free, etc.)
- **Vulnerable function**: The function where the crash occurred (from stack trace)
- **Vulnerable file**: The source file path (from stack trace)
- **Line number**: Exact line where crash happened
- **DEDUP_TOKEN**: Unique identifier for this crash
- **Stack trace**: Full call stack for understanding control flow

Example crash log parsing:
```
ERROR: AddressSanitizer: heap-buffer-overflow
READ of size 45 at 0x50400000033c
#0 __asan_memcpy /src/llvm-project/compiler-rt/lib/asan/...
#1 cr_buf_read /src/curl/lib/sendf.c:1298:5    <-- Vulnerable function
#2 Curl_creader_read /src/curl/lib/sendf.c:542:10
...
SUMMARY: AddressSanitizer: heap-buffer-overflow /src/curl/lib/sendf.c:1298:5 in cr_buf_read
```

From this, extract:
- Error type: heap-buffer-overflow
- File: lib/sendf.c (relative to project root)
- Function: cr_buf_read
- Line: 1298

### Step 2: Identify Location Types

For each vulnerability location, determine its type:

**crash_site**: The point where the program crashes or error is detected
- This is typically the location from the crash log stack trace
- MUST be in project code
- Example: The function where buffer overflow READ occurs

**root_cause**: The underlying defect that enables the vulnerability
- This is where the actual bug/defect exists
- May be different from crash site (e.g., missing bounds check, incorrect allocation)
- Example: Where buffer is allocated too small, or where bounds check is missing
- **IMPORTANT: Analyze patches to identify root_cause** - patches show exactly what was fixed

**EXCLUDE from both crash_site and root_cause:**
- Harness files (fuzzing harness code, e.g., `*_fuzzer.c`, `*_fuzzer.cc`, `LLVMFuzzerTestOneInput`)
- ASan/sanitizer functions (`__asan_*`, `__msan_*`, `__ubsan_*`)
- Standard library functions (`memcpy`, `malloc`, `free`, `strlen`, etc.)
- Compiler runtime functions (`__interceptor_*`, `llvm-project/compiler-rt/*`)
- Only include actual project source code locations

### Step 3: Locate Vulnerable Code

- Use the file path from crash log to find the vulnerable source file
- Read the vulnerable file around the crash line number
- Identify the exact vulnerability location
- Note the function name, line numbers, and code context

### Step 4: Analyze POV (if available)

- Check the CPV's blobs/ directory for POV files
- These are test inputs that trigger the vulnerability
- Note if POV structure provides hints about vulnerability type

### Step 5: Analyze Patches (CRITICAL for root_cause)

- Check the CPV's patches/ directory for patch files
- **Patches are the primary source for identifying root_cause location**
- Analyze what was changed to fix the vulnerability:
  - Which file(s) were modified?
  - Which function(s) were changed?
  - What line(s) were added/removed?
- The patch location typically indicates the root_cause location
- If patch differs from crash site, you likely have separate crash_site and root_cause

### Step 6: Identify Vulnerability Origin and Reference

**⚠️ CRITICAL: DO NOT GUESS. ONLY USE CONCRETE EVIDENCE. ⚠️**

**origin**: Source of the vulnerability
- `1-day`: Real-world vulnerability with confirmed official reference (CVE, OSV, GHSA, bug tracker, etc.)
- `synthetic`: Everything else (DEFAULT)

**🚨 RULES FOR 1-day CLASSIFICATION 🚨**

1. **Start with patch files** - Extract key information:
   - Function name(s) being patched
   - File path(s) being modified
   - Type of fix (bounds check, null check, etc.)
   - Any vulnerability ID already mentioned in patch (CVE, OSV, GHSA, issue #)

2. **Search based on patch content:**
   - Use WebSearch with: project name + function name + vulnerability keywords
   - Example: `libtiff TIFFReadRawStrip1 CVE` or `libtiff TIFFReadRawStrip1 vulnerability`
   - Look for an EXACT match - same function, same fix

3. **Confirm the match:**
   - The reference must describe the EXACT same vulnerability
   - Same function name
   - Same type of bug
   - Same file/location
   - If it's just "similar" but not exact → `synthetic`

4. **DO NOT mark as `1-day` based on:**
   - ❌ "Similar looking" vulnerabilities
   - ❌ Same project but different function
   - ❌ Same bug type but different location
   - ❌ Guessing or "it might be"

5. **If no EXACT match found → `synthetic`**

**How to check (in order):**
1. Read patch files - note function names, file paths, fix type
2. Check if vulnerability ID already in patch content/filename (CVE, OSV, issue #)
3. Check `.aixcc/meta.yaml` for existing reference
4. WebSearch: `[project] [function_name] vulnerability` or `CVE` (only if needed)
5. WebFetch: Get release_date from the reference source
6. If no exact match → `synthetic`

**release_date**:
- For 1-day: Fetch from the official reference source (NVD, OSV, GitHub advisory, etc.)
- For synthetic: Use `02/01/2026`

**references**: List of URLs to vulnerability details (REQUIRED for 1-day)
- Include all confirmed references (a single vulnerability may have multiple)
- Valid reference sources:
  - CVE: `https://nvd.nist.gov/vuln/detail/CVE-XXXX-XXXXX`
  - OSV: `https://osv.dev/vulnerability/OSV-XXXX-XXXX`
  - GHSA: `https://github.com/advisories/GHSA-xxxx-xxxx-xxxx`
  - Project bug tracker: `https://github.com/project/issues/XXX`
  - Other official security advisories
- Example: A vulnerability may have both CVE and GHSA references

### Step 7: Identify CWE

Based on the vulnerability type, identify relevant CWEs:
- Heap buffer overflow -> CWE-122, CWE-787
- Stack buffer overflow -> CWE-121, CWE-787
- Use-after-free -> CWE-416
- NULL pointer dereference -> CWE-476
- Integer overflow -> CWE-190
- Command injection -> CWE-77
- Path traversal -> CWE-22
- Format string -> CWE-134

## Output Format

Provide a markdown document with this structure:

```markdown
# Vulnerability Analysis for {cpv_id}

## Summary
- **CPV ID**: {cpv_id}
- **Harness**: {harness_name}
- **Vulnerability Type**: <type from crash log>
- **Origin**: <1-day or synthetic>
- **Release Date**: <MM/DD/YYYY>
- **References**: <list of CVE/OSV/GHSA URLs if available>

## Crash Log Analysis
- **Error Type**: <AddressSanitizer error type>
- **Crash Location**: <file>:<line>
- **Vulnerable Function**: <function name>
- **DEDUP Token**: <dedup token from crash log>

### Stack Trace
<relevant stack trace snippet>

## Vulnerable Code Location(s)

### Location 1 (crash_site)
- **Type**: crash_site
- **File Path**: <path from project root>
- **Function**: <function name>
- **Line Range**: <start>-<end>
- **Column Range**: <start>-<end>

### Location 2 (root_cause) - if different from crash_site
- **Type**: root_cause
- **File Path**: <path from project root>
- **Function**: <function name>
- **Line Range**: <start>-<end>
- **Column Range**: <start>-<end>

### Code Context
<vulnerable code snippet>

## CWE Classification
- CWE-XXX: <description>
- CWE-YYY: <description>

## Vulnerability Description
<Clear description of the vulnerability, its cause, and impact>

## POV Analysis (if available)
<Analysis of proof-of-vulnerability test case>

## Patch Analysis (if available)
<Analysis of how the vulnerability was fixed>

## Recommendations
- Suggested CWEs: [CWE-XXX, CWE-YYY]
- Vulnerability name: <concise name>
```

## Important Notes

- **ALWAYS read and analyze the crash log first** - it contains the most critical information
- **ONLY analyze crash logs from the specified CPV's logs directory**
- Use Grep and Glob to find relevant source code files (NOT other CPVs' crash logs)
- Read actual source code to verify vulnerability locations
- Cite specific file paths and line numbers
- Be precise about code locations (line and column numbers)
- Distinguish between crash_site and root_cause locations
