"""CWE mapping from specific vulnerability types to general classes.

This module provides a mapping from specific CWE IDs to their general
vulnerability classes for hint generation at different levels.
"""

from typing import Dict, Tuple

# Mapping from specific CWE to (General Class, Description)
CWE_TO_GENERAL_CLASS: Dict[str, Tuple[str, str]] = {
    # Memory Safety Issues
    "CWE-119": (
        "Memory Safety Issue",
        "Improper Restriction of Operations within the Bounds of a Memory Buffer",
    ),
    "CWE-120": (
        "Memory Safety Issue",
        "Buffer Copy without Checking Size of Input ('Classic Buffer Overflow')",
    ),
    "CWE-121": ("Memory Safety Issue", "Stack-based Buffer Overflow"),
    "CWE-122": ("Memory Safety Issue", "Heap-based Buffer Overflow"),
    "CWE-123": ("Memory Safety Issue", "Write-what-where Condition"),
    "CWE-124": ("Memory Safety Issue", "Buffer Underwrite ('Buffer Underflow')"),
    "CWE-125": ("Memory Safety Issue", "Out-of-bounds Read"),
    "CWE-126": ("Memory Safety Issue", "Buffer Over-read"),
    "CWE-127": ("Memory Safety Issue", "Buffer Under-read"),
    "CWE-787": ("Memory Safety Issue", "Out-of-bounds Write"),
    "CWE-788": ("Memory Safety Issue", "Access of Memory Location After End of Buffer"),
    "CWE-789": ("Memory Safety Issue", "Memory Allocation with Excessive Size Value"),
    "CWE-401": (
        "Memory Safety Issue",
        "Missing Release of Memory after Effective Lifetime",
    ),
    "CWE-415": ("Memory Safety Issue", "Double Free"),
    "CWE-416": ("Memory Safety Issue", "Use After Free"),
    "CWE-476": ("Memory Safety Issue", "NULL Pointer Dereference"),
    "CWE-590": ("Memory Safety Issue", "Free of Memory not on the Heap"),
    "CWE-761": ("Memory Safety Issue", "Free of Pointer not at Start of Buffer"),
    # Input Validation Issues
    "CWE-20": ("Input Validation Issue", "Improper Input Validation"),
    "CWE-22": (
        "Input Validation Issue",
        "Improper Limitation of a Pathname to a Restricted Directory ('Path Traversal')",
    ),
    "CWE-23": ("Input Validation Issue", "Relative Path Traversal"),
    "CWE-28": ("Input Validation Issue", r"Path Traversal: '..\filedir'"),
    "CWE-29": ("Input Validation Issue", r"Path Traversal: '\..\filename'"),
    "CWE-35": ("Input Validation Issue", "Path Traversal: '.../...//'"),
    "CWE-74": (
        "Input Validation Issue",
        "Improper Neutralization of Special Elements in Output Used by a Downstream Component ('Injection')",
    ),
    "CWE-77": (
        "Input Validation Issue",
        "Improper Neutralization of Special Elements used in a Command ('Command Injection')",
    ),
    "CWE-78": (
        "Input Validation Issue",
        "Improper Neutralization of Special Elements used in an OS Command ('OS Command Injection')",
    ),
    "CWE-79": (
        "Input Validation Issue",
        "Improper Neutralization of Input During Web Page Generation ('Cross-site Scripting')",
    ),
    "CWE-89": (
        "Input Validation Issue",
        "Improper Neutralization of Special Elements used in an SQL Command ('SQL Injection')",
    ),
    "CWE-90": (
        "Input Validation Issue",
        "Improper Neutralization of Special Elements used in an LDAP Query ('LDAP Injection')",
    ),
    "CWE-94": (
        "Input Validation Issue",
        "Improper Control of Generation of Code ('Code Injection')",
    ),
    "CWE-129": ("Input Validation Issue", "Improper Validation of Array Index"),
    "CWE-134": ("Input Validation Issue", "Use of Externally-Controlled Format String"),
    "CWE-190": ("Input Validation Issue", "Integer Overflow or Wraparound"),
    "CWE-191": ("Input Validation Issue", "Integer Underflow (Wrap or Wraparound)"),
    "CWE-193": ("Input Validation Issue", "Off-by-one Error"),
    "CWE-502": ("Input Validation Issue", "Deserialization of Untrusted Data"),
    "CWE-610": (
        "Input Validation Issue",
        "Externally Controlled Reference to a Resource in Another Sphere",
    ),
    "CWE-611": (
        "Input Validation Issue",
        "Improper Restriction of XML External Entity Reference",
    ),
    "CWE-643": (
        "Input Validation Issue",
        "Improper Neutralization of Data within XPath Expressions ('XPath Injection')",
    ),
    "CWE-680": ("Input Validation Issue", "Integer Overflow to Buffer Overflow"),
    "CWE-777": ("Input Validation Issue", "Regular Expression without Anchors"),
    "CWE-829": (
        "Input Validation Issue",
        "Inclusion of Functionality from Untrusted Control Sphere",
    ),
    "CWE-917": (
        "Input Validation Issue",
        "Improper Neutralization of Special Elements used in an Expression Language Statement ('Expression Language Injection')",
    ),
    "CWE-918": ("Input Validation Issue", "Server-Side Request Forgery (SSRF)"),
    # Resource Management Issues
    "CWE-400": ("Resource Management Issue", "Uncontrolled Resource Consumption"),
    "CWE-404": ("Resource Management Issue", "Improper Resource Shutdown or Release"),
    "CWE-407": ("Resource Management Issue", "Inefficient Algorithmic Complexity"),
    "CWE-770": (
        "Resource Management Issue",
        "Allocation of Resources Without Limits or Throttling",
    ),
    "CWE-772": (
        "Resource Management Issue",
        "Missing Release of Resource after Effective Lifetime",
    ),
    "CWE-834": ("Resource Management Issue", "Excessive Iteration"),
    "CWE-835": (
        "Resource Management Issue",
        "Loop with Unreachable Exit Condition ('Infinite Loop')",
    ),
    "CWE-920": (
        "Resource Management Issue",
        "Improper Restriction of Power Consumption",
    ),
    "CWE-1333": (
        "Resource Management Issue",
        "Inefficient Regular Expression Complexity",
    ),
    # Concurrency Issues
    "CWE-362": (
        "Concurrency Issue",
        "Concurrent Execution using Shared Resource with Improper Synchronization ('Race Condition')",
    ),
    "CWE-366": ("Concurrency Issue", "Race Condition within a Thread"),
    "CWE-367": (
        "Concurrency Issue",
        "Time-of-check Time-of-use (TOCTOU) Race Condition",
    ),
    "CWE-662": ("Concurrency Issue", "Improper Synchronization"),
    "CWE-667": ("Concurrency Issue", "Improper Locking"),
    # Information Exposure Issues
    "CWE-200": (
        "Information Exposure Issue",
        "Exposure of Sensitive Information to an Unauthorized Actor",
    ),
    "CWE-209": (
        "Information Exposure Issue",
        "Generation of Error Message Containing Sensitive Information",
    ),
    "CWE-532": (
        "Information Exposure Issue",
        "Insertion of Sensitive Information into Log File",
    ),
    # Cryptographic Issues
    "CWE-327": (
        "Cryptographic Issue",
        "Use of a Broken or Risky Cryptographic Algorithm",
    ),
    "CWE-328": ("Cryptographic Issue", "Use of Weak Hash"),
    "CWE-330": ("Cryptographic Issue", "Use of Insufficiently Random Values"),
    "CWE-338": (
        "Cryptographic Issue",
        "Use of Cryptographically Weak Pseudo-Random Number Generator (PRNG)",
    ),
    # Authentication/Authorization Issues
    "CWE-287": ("Authentication Issue", "Improper Authentication"),
    "CWE-306": ("Authentication Issue", "Missing Authentication for Critical Function"),
    "CWE-285": ("Authorization Issue", "Improper Authorization"),
    "CWE-732": (
        "Authorization Issue",
        "Incorrect Permission Assignment for Critical Resource",
    ),
    # Code Quality Issues
    "CWE-252": ("Code Quality Issue", "Unchecked Return Value"),
    "CWE-457": ("Code Quality Issue", "Use of Uninitialized Variable"),
    "CWE-484": ("Code Quality Issue", "Omitted Break Statement in Switch"),
    "CWE-561": ("Code Quality Issue", "Dead Code"),
    "CWE-563": ("Code Quality Issue", "Assignment to Variable without Use"),
    "CWE-695": ("Code Quality Issue", "Use of Low-Level Functionality"),
}


def get_general_class(cwe_id: str) -> Tuple[str, str]:
    """Get the general vulnerability class for a specific CWE ID.

    Args:
        cwe_id: CWE identifier (e.g., "CWE-122" or "122")

    Returns:
        Tuple of (general_class, description). If CWE not found, returns
        ("Unknown Vulnerability", "Unknown vulnerability type").
    """
    # Normalize CWE ID format
    if not cwe_id.startswith("CWE-"):
        cwe_id = f"CWE-{cwe_id}"

    return CWE_TO_GENERAL_CLASS.get(
        cwe_id, ("Unknown Vulnerability", "Unknown vulnerability type")
    )


def get_cwe_name(cwe_id: str) -> str:
    """Get the descriptive name for a CWE ID.

    Args:
        cwe_id: CWE identifier (e.g., "CWE-122" or "122")

    Returns:
        Descriptive name of the CWE
    """
    _, description = get_general_class(cwe_id)
    return description
