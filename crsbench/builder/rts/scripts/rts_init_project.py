#!/usr/bin/env python3
"""
Unified RTS (Regression Test Selection) project initialization script.

Performs one-time, project-specific setup for RTS tools. This script is
called ONCE per project (guarded by a sentinel file in the wrapper).

For JVM projects:
- Modifies pom.xml files to add surefire and RTS tool plugins
- Configures surefire settings for RTS compatibility (excludesFile, fork settings)
- Parses INCLUDE_TESTS and EXCLUDE_TESTS from $SRC/test.sh and adds to surefire
- Cleans up existing RTS artifacts
- Commits changes to git

For C/C++ projects:
- Configures BinaryRTS (ctags, pin, binary-rts are assumed pre-installed)
- No project-specific init currently required beyond tool presence

Assumes:
- JVM: Maven artifacts (JcgEks) are already installed in the base image.
- C/C++: /opt/pin, /opt/binary-rts, and ctags are already installed in the base image.

Usage:
    python3 rts_init_project.py <project_path> --tool <tool_name>

Environment variables:
    FUZZING_LANGUAGE: Language of the project (jvm, c, c++)
    RTS_TOOL: Fallback for --tool if not provided via CLI
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# JVM constants
# ---------------------------------------------------------------------------

RTS_TOOLS = {
    "jcgeks": {
        "group_id": "org.jcgeks",
        "artifact_id": "jcgeks-maven-plugin",
        "version": "1.0.0",
    },
    "openclover": {
        "group_id": "org.openclover",
        "artifact_id": "clover-maven-plugin",
        "version": "4.5.2",
    },
}

MAVEN_NAMESPACE = "http://maven.apache.org/POM/4.0.0"
MAVEN_SETTINGS_NAMESPACE = "http://maven.apache.org/SETTINGS/1.0.0"
SUREFIRE_VERSION = "2.22.2"

# OpenClover generates inner classes that must be excluded from surefire.
OPENCLOVER_EXCLUDE_PATTERN = "**/*$__CLR*"


# Resolve test.sh path: /src/test.sh first, then $OSS_CRS_PROJ_PATH/test.sh fallback.
def _resolve_test_sh_path() -> str:
    if os.path.isfile("/src/test.sh"):
        return "/src/test.sh"
    proj_path = os.environ.get("OSS_CRS_PROJ_PATH", "")
    if proj_path and os.path.isfile(os.path.join(proj_path, "test.sh")):
        return os.path.join(proj_path, "test.sh")
    return "/src/test.sh"  # default (will warn if missing)


TEST_SH_PATH = _resolve_test_sh_path()

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def run_cmd(cmd: str, cwd: Optional[str] = None, timeout: int = 3600) -> bool:
    """Execute a shell command and return success status."""
    try:
        subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[ERROR] Command failed: {cmd}")
        print(f"[ERROR] {e}")
        return False


# ---------------------------------------------------------------------------
# XML formatting helper
# ---------------------------------------------------------------------------


def _ensure_xmllint() -> bool:
    """Ensure xmllint is installed, install via apt if not available."""
    try:
        subprocess.run(["xmllint", "--version"], capture_output=True, timeout=10)
        return True
    except FileNotFoundError:
        print("[INFO] xmllint not found, installing libxml2-utils...")
        try:
            subprocess.run(["apt-get", "update"], capture_output=True, timeout=120)
            result = subprocess.run(
                ["apt-get", "install", "-y", "libxml2-utils"],
                capture_output=True,
                timeout=120,
            )
            if result.returncode == 0:
                print("[INFO] xmllint installed successfully")
                return True
            print("[WARNING] Failed to install xmllint")
            return False
        except Exception as e:
            print(f"[WARNING] Failed to install xmllint: {e}")
            return False
    except Exception:
        return False


def _format_xml(file_path: str) -> bool:
    """Format XML file using xmllint (best-effort)."""
    try:
        result = subprocess.run(
            ["xmllint", "--format", file_path],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            with open(file_path, "wb") as f:
                f.write(result.stdout)
            return True
        print(f"[WARNING] xmllint failed for {file_path}: {result.stderr.decode()}")
        return False
    except FileNotFoundError:
        print("[WARNING] xmllint not available, skipping formatting")
        return False
    except Exception as e:
        print(f"[WARNING] Failed to format {file_path}: {e}")
        return False


# =========================================================================
# JVM: pom.xml manipulation
# =========================================================================


def find_pom_files(project_path: str) -> List[str]:
    """Find all pom.xml files in the project directory."""
    pom_files = []
    for root, _, files in os.walk(project_path):
        for file in files:
            if file == "pom.xml":
                pom_files.append(os.path.join(root, file))
    return pom_files


def parse_test_patterns_from_test_sh(
    test_sh_path: str,
) -> Tuple[List[str], List[str]]:
    """
    Parse INCLUDE_TESTS and EXCLUDE_TESTS variables from test.sh.

    Expected format:
        INCLUDE_TESTS="**/Test1.java,**/Test2.java"
        EXCLUDE_TESTS="**/FailingTest.java"
    """
    include_patterns: List[str] = []
    exclude_patterns: List[str] = []

    if not os.path.isfile(test_sh_path):
        print(f"[WARNING] test.sh not found: {test_sh_path}")
        return include_patterns, exclude_patterns

    try:
        with open(test_sh_path, "r") as f:
            content = f.read()

        include_match = re.search(r'INCLUDE_TESTS=["\']([^"\']*)["\']', content)
        if include_match:
            val = include_match.group(1).strip()
            if val:
                include_patterns = [p.strip() for p in val.split(",") if p.strip()]
                print(
                    f"[INFO] Parsed {len(include_patterns)} INCLUDE_TESTS pattern(s) from test.sh"
                )

        exclude_match = re.search(r'EXCLUDE_TESTS=["\']([^"\']*)["\']', content)
        if exclude_match:
            val = exclude_match.group(1).strip()
            if val:
                exclude_patterns = [p.strip() for p in val.split(",") if p.strip()]
                print(
                    f"[INFO] Parsed {len(exclude_patterns)} EXCLUDE_TESTS pattern(s) from test.sh"
                )
    except Exception as e:
        print(f"[ERROR] Failed to parse test.sh {test_sh_path}: {e}")

    return include_patterns, exclude_patterns


# -- Plugin node builders --------------------------------------------------


def _create_plugin_node(group_id: str, artifact_id: str, version: str) -> ET.Element:
    """Create a Maven plugin XML element."""
    plugin = ET.Element("plugin")
    ET.SubElement(plugin, "groupId").text = group_id
    ET.SubElement(plugin, "artifactId").text = artifact_id
    ET.SubElement(plugin, "version").text = version
    return plugin


def _create_surefire_plugin(project_name: str, tool_name: str) -> ET.Element:
    """Create maven-surefire-plugin element with RTS configuration."""
    plugin = _create_plugin_node(
        "org.apache.maven.plugins",
        "maven-surefire-plugin",
        SUREFIRE_VERSION,
    )
    # OpenClover handles test selection internally; no excludesFile needed.
    if tool_name != "openclover":
        configuration = ET.SubElement(plugin, "configuration")
        excludes_file = ET.SubElement(configuration, "excludesFile")
        excludes_file.text = f"/tmp/{project_name}_{tool_name}Excludes"
    return plugin


def _create_rts_plugin(tool_name: str) -> ET.Element:
    """Create RTS tool plugin element (JcgEks or OpenClover)."""
    cfg = RTS_TOOLS[tool_name]
    plugin = _create_plugin_node(cfg["group_id"], cfg["artifact_id"], cfg["version"])

    if tool_name == "openclover":
        configuration = ET.SubElement(plugin, "configuration")
        ET.SubElement(configuration, "instrumentation").text = "method"
    else:
        executions = ET.SubElement(plugin, "executions")
        execution = ET.SubElement(executions, "execution")
        ET.SubElement(execution, "id").text = tool_name
        goals = ET.SubElement(execution, "goals")
        ET.SubElement(goals, "goal").text = "select"
        ET.SubElement(goals, "goal").text = "restore"

    return plugin


# -- pom.xml modifiers -----------------------------------------------------


def _delete_surefire_config_element(
    directory: str, target_name: str, replace: Optional[str] = None
) -> None:
    """Delete or replace a surefire configuration element in all pom.xml files."""
    ns = "{" + MAVEN_NAMESPACE + "}"

    for pom_path in find_pom_files(directory):
        try:
            tree = ET.parse(pom_path)
            root = tree.getroot()
            ET.register_namespace("", MAVEN_NAMESPACE)

            modified = False
            for plugin in root.findall(".//" + ns + "plugin"):
                artifact_id = plugin.find(".//" + ns + "artifactId")
                if (
                    artifact_id is not None
                    and artifact_id.text == "maven-surefire-plugin"
                ):
                    configuration = plugin.find(".//" + ns + "configuration")
                    if configuration is not None:
                        target = configuration.find(ns + target_name)
                        if target is not None:
                            if replace is None:
                                configuration.remove(target)
                            else:
                                target.text = replace
                            modified = True
                        elif replace is not None:
                            target = ET.SubElement(configuration, target_name)
                            target.text = replace
                            modified = True

            if modified:
                tree.write(pom_path, encoding="utf-8", xml_declaration=True)
                _format_xml(pom_path)

        except ET.ParseError as e:
            print(f"[WARNING] Failed to parse {pom_path}: {e}")


def _has_existing_clover(root: ET.Element, ns: str) -> bool:
    """Check if clover is already present in the pom.xml as a plugin or dependency."""
    clover_group_ids = {
        "org.openclover",
        "org.apache.maven.plugins",
        "com.atlassian.clover",
    }
    clover_artifact_patterns = {
        "clover-maven-plugin",
        "maven-clover-plugin",
        "clover",
    }

    for plugin in root.findall(".//" + ns + "plugin"):
        group_id = plugin.find(ns + "groupId")
        artifact_id = plugin.find(ns + "artifactId")
        if group_id is None:
            group_id = plugin.find("groupId")
        if artifact_id is None:
            artifact_id = plugin.find("artifactId")
        group_text = (
            group_id.text.strip() if group_id is not None and group_id.text else ""
        )
        artifact_text = (
            artifact_id.text.strip()
            if artifact_id is not None and artifact_id.text
            else ""
        )
        if group_text in clover_group_ids and "clover" in artifact_text.lower():
            return True
        if artifact_text in clover_artifact_patterns:
            return True
        if "clover" in artifact_text.lower() and "plugin" in artifact_text.lower():
            return True

    for dep in root.findall(".//" + ns + "dependency"):
        group_id = dep.find(ns + "groupId")
        artifact_id = dep.find(ns + "artifactId")
        if group_id is None:
            group_id = dep.find("groupId")
        if artifact_id is None:
            artifact_id = dep.find("artifactId")
        group_text = (
            group_id.text.strip() if group_id is not None and group_id.text else ""
        )
        artifact_text = (
            artifact_id.text.strip()
            if artifact_id is not None and artifact_id.text
            else ""
        )
        if group_text in clover_group_ids:
            return True
        if "clover" in artifact_text.lower():
            return True

    return False


def _add_rts_plugins_to_pom(pom_path: str, project_name: str, tool_name: str) -> bool:
    """Add RTS tool plugin and configure surefire excludesFile in a pom.xml file."""
    ns = "{" + MAVEN_NAMESPACE + "}"

    try:
        tree = ET.parse(pom_path)
        root = tree.getroot()
        ET.register_namespace("", MAVEN_NAMESPACE)

        # Skip OpenClover injection if clover is already present
        if tool_name == "openclover" and _has_existing_clover(root, ns):
            print(
                f"[INFO] Clover already exists in {pom_path}, skipping OpenClover injection"
            )
            return True

        build = root.find(ns + "build")
        if build is None:
            build = root.find("build")
        if build is None:
            print(f"[WARNING] No <build> element found in {pom_path}, skipping")
            return True

        plugins = build.find(ns + "plugins")
        if plugins is None:
            plugins = build.find("plugins")
        if plugins is None:
            print(
                f"[WARNING] No <build><plugins> element found in {pom_path}, skipping"
            )
            return True

        plugin_ns = ns if plugins.find(ns + "plugin") is not None else ""

        # --- surefire excludesFile (not needed for openclover) ---
        if tool_name != "openclover":
            surefire_plugin = None
            for plugin in plugins:
                if plugin.tag in (plugin_ns + "plugin", "plugin"):
                    aid = plugin.find(plugin_ns + "artifactId")
                    if aid is None:
                        aid = plugin.find("artifactId")
                    if aid is not None and aid.text == "maven-surefire-plugin":
                        surefire_plugin = plugin
                        break

            if surefire_plugin is not None:
                configuration = surefire_plugin.find(plugin_ns + "configuration")
                if configuration is None:
                    configuration = surefire_plugin.find("configuration")
                if configuration is None:
                    configuration = ET.SubElement(surefire_plugin, "configuration")

                excludes_file = configuration.find(plugin_ns + "excludesFile")
                if excludes_file is None:
                    excludes_file = configuration.find("excludesFile")
                if excludes_file is None:
                    excludes_file = ET.SubElement(configuration, "excludesFile")
                excludes_file.text = f"/tmp/{project_name}_{tool_name}Excludes"
                print("[INFO] Added excludesFile to existing surefire plugin")
            else:
                plugins.append(_create_surefire_plugin(project_name, tool_name))
                print("[INFO] Created new surefire plugin")

        # --- RTS tool plugin ---
        rts_config = RTS_TOOLS.get(tool_name)
        rts_exists = False
        if rts_config:
            for plugin in plugins:
                if plugin.tag in (plugin_ns + "plugin", "plugin"):
                    aid = plugin.find(plugin_ns + "artifactId")
                    if aid is None:
                        aid = plugin.find("artifactId")
                    if aid is not None and aid.text == rts_config["artifact_id"]:
                        rts_exists = True
                        break

        if not rts_exists:
            plugins.append(_create_rts_plugin(tool_name))
            print(f"[INFO] Added {tool_name} plugin")
        else:
            print(f"[INFO] {tool_name} plugin already exists")

        tree.write(pom_path, encoding="utf-8", xml_declaration=True)
        _format_xml(pom_path)
        return True

    except Exception as e:
        print(f"[ERROR] Failed to modify {pom_path}: {e}")
        return False


def _add_patterns_to_surefire(
    pom_path: str,
    patterns: List[str],
    element_name: str,
    child_name: str,
) -> bool:
    """Add include or exclude patterns to surefire plugin configuration."""
    if not patterns:
        return True

    ns = "{" + MAVEN_NAMESPACE + "}"

    try:
        tree = ET.parse(pom_path)
        root = tree.getroot()
        ET.register_namespace("", MAVEN_NAMESPACE)

        build = root.find(ns + "build")
        if build is None:
            build = root.find("build")
        if build is None:
            return True

        plugins = build.find(ns + "plugins")
        if plugins is None:
            plugins = build.find("plugins")
        if plugins is None:
            return True

        plugin_ns = ns if plugins.find(ns + "plugin") is not None else ""

        surefire_plugin = None
        for plugin in plugins:
            if plugin.tag in (plugin_ns + "plugin", "plugin"):
                aid = plugin.find(plugin_ns + "artifactId")
                if aid is None:
                    aid = plugin.find("artifactId")
                if aid is not None and aid.text == "maven-surefire-plugin":
                    surefire_plugin = plugin
                    break

        if surefire_plugin is None:
            surefire_plugin = ET.Element("plugin")
            ET.SubElement(surefire_plugin, "groupId").text = "org.apache.maven.plugins"
            ET.SubElement(surefire_plugin, "artifactId").text = "maven-surefire-plugin"
            plugins.append(surefire_plugin)
            print(f"[INFO] Created new surefire plugin for {element_name}")

        configuration = surefire_plugin.find(plugin_ns + "configuration")
        if configuration is None:
            configuration = surefire_plugin.find("configuration")
        if configuration is None:
            configuration = ET.SubElement(surefire_plugin, "configuration")

        container = configuration.find(plugin_ns + element_name)
        if container is None:
            container = configuration.find(element_name)
        if container is None:
            container = ET.SubElement(configuration, element_name)

        existing = {child.text.strip() for child in container if child.text}

        added = 0
        for pattern in patterns:
            if pattern not in existing:
                ET.SubElement(container, child_name).text = pattern
                existing.add(pattern)
                added += 1

        if added:
            tree.write(pom_path, encoding="utf-8", xml_declaration=True)
            _format_xml(pom_path)
            print(f"[INFO] Added {added} {child_name} pattern(s) to {pom_path}")

        return True

    except Exception as e:
        print(f"[ERROR] Failed to add {element_name} to {pom_path}: {e}")
        return False


def _add_openclover_dependency_to_pom(pom_path: str) -> bool:
    """Add OpenClover dependency explicitly to pom.xml's <dependencies> section."""
    ns = "{" + MAVEN_NAMESPACE + "}"
    oc_group = "org.openclover"
    oc_artifact = "clover"
    oc_version = RTS_TOOLS["openclover"]["version"]

    try:
        tree = ET.parse(pom_path)
        root = tree.getroot()
        ET.register_namespace("", MAVEN_NAMESPACE)

        dependencies = root.find(ns + "dependencies")
        if dependencies is None:
            dependencies = root.find("dependencies")
        if dependencies is None:
            dependencies = ET.Element("dependencies")
            build = root.find(ns + "build")
            if build is None:
                build = root.find("build")
            if build is not None:
                root.insert(list(root).index(build), dependencies)
            else:
                root.append(dependencies)

        dep_ns = ns if dependencies.find(ns + "dependency") is not None else ""
        for dep in dependencies:
            if dep.tag in (dep_ns + "dependency", "dependency"):
                gid = dep.find(dep_ns + "groupId") or dep.find("groupId")
                aid = dep.find(dep_ns + "artifactId") or dep.find("artifactId")
                g = gid.text.strip() if gid is not None and gid.text else ""
                a = aid.text.strip() if aid is not None and aid.text else ""
                if g in ("org.openclover", "com.atlassian.clover") and a == "clover":
                    print(f"[INFO] Clover dependency already exists in {pom_path}")
                    return True

        dep_elem = ET.SubElement(dependencies, "dependency")
        ET.SubElement(dep_elem, "groupId").text = oc_group
        ET.SubElement(dep_elem, "artifactId").text = oc_artifact
        ET.SubElement(dep_elem, "version").text = oc_version

        tree.write(pom_path, encoding="utf-8", xml_declaration=True)
        _format_xml(pom_path)
        print(f"[INFO] Added OpenClover dependency to {pom_path}")
        return True

    except Exception as e:
        print(f"[ERROR] Failed to add OpenClover dependency to {pom_path}: {e}")
        return False


def _add_openclover_to_plugin_management(pom_path: str) -> bool:
    """Add OpenClover to pluginManagement and disable Atlassian Clover."""
    ns = "{" + MAVEN_NAMESPACE + "}"
    oc = RTS_TOOLS["openclover"]

    try:
        tree = ET.parse(pom_path)
        root = tree.getroot()
        ET.register_namespace("", MAVEN_NAMESPACE)

        build = root.find(ns + "build") or root.find("build")
        if build is None:
            build = ET.SubElement(root, "build")

        pm = build.find(ns + "pluginManagement") or build.find("pluginManagement")
        if pm is None:
            pm = ET.SubElement(build, "pluginManagement")

        pm_plugins = pm.find(ns + "plugins") or pm.find("plugins")
        if pm_plugins is None:
            pm_plugins = ET.SubElement(pm, "plugins")

        plugin_ns = ns if pm_plugins.find(ns + "plugin") is not None else ""

        openclover_plugin = None
        atlassian_plugin = None
        for plugin in pm_plugins:
            if plugin.tag in (plugin_ns + "plugin", "plugin"):
                aid = plugin.find(plugin_ns + "artifactId") or plugin.find("artifactId")
                gid = plugin.find(plugin_ns + "groupId") or plugin.find("groupId")
                if aid is not None and aid.text == "clover-maven-plugin":
                    g = gid.text if gid is not None else ""
                    if g == "org.openclover":
                        openclover_plugin = plugin
                    elif g == "com.atlassian.maven.plugins":
                        atlassian_plugin = plugin

        # Add or update OpenClover
        if openclover_plugin is not None:
            ver = openclover_plugin.find(
                plugin_ns + "version"
            ) or openclover_plugin.find("version")
            if ver is None:
                ver = ET.SubElement(openclover_plugin, "version")
            ver.text = oc["version"]
        else:
            new_p = ET.SubElement(pm_plugins, "plugin")
            ET.SubElement(new_p, "groupId").text = oc["group_id"]
            ET.SubElement(new_p, "artifactId").text = oc["artifact_id"]
            ET.SubElement(new_p, "version").text = oc["version"]

        # Disable Atlassian Clover
        if atlassian_plugin is not None:
            cfg = atlassian_plugin.find(
                plugin_ns + "configuration"
            ) or atlassian_plugin.find("configuration")
            if cfg is None:
                cfg = ET.SubElement(atlassian_plugin, "configuration")
            skip = cfg.find(plugin_ns + "skip") or cfg.find("skip")
            if skip is None:
                skip = ET.SubElement(cfg, "skip")
            skip.text = "true"
        else:
            ap = ET.SubElement(pm_plugins, "plugin")
            ET.SubElement(ap, "groupId").text = "com.atlassian.maven.plugins"
            ET.SubElement(ap, "artifactId").text = "clover-maven-plugin"
            cfg = ET.SubElement(ap, "configuration")
            ET.SubElement(cfg, "skip").text = "true"

        tree.write(pom_path, encoding="utf-8", xml_declaration=True)
        _format_xml(pom_path)
        return True

    except Exception as e:
        print(
            f"[ERROR] Failed to add OpenClover to pluginManagement in {pom_path}: {e}"
        )
        return False


def _configure_maven_settings_for_openclover() -> bool:
    """Configure Maven settings.xml to include org.openclover pluginGroup."""
    m2_dir = os.path.expanduser("~/.m2")
    settings_path = os.path.join(m2_dir, "settings.xml")
    ns = "{" + MAVEN_SETTINGS_NAMESPACE + "}"

    try:
        os.makedirs(m2_dir, exist_ok=True)

        if os.path.exists(settings_path):
            tree = ET.parse(settings_path)
            root = tree.getroot()
            ET.register_namespace("", MAVEN_SETTINGS_NAMESPACE)

            plugin_groups = root.find(ns + "pluginGroups") or root.find("pluginGroups")
            if plugin_groups is None:
                plugin_groups = ET.SubElement(root, "pluginGroups")

            existing = {pg.text.strip() for pg in plugin_groups if pg.text}
            if "org.openclover" not in existing:
                ET.SubElement(plugin_groups, "pluginGroup").text = "org.openclover"
                tree.write(settings_path, encoding="utf-8", xml_declaration=True)
                _format_xml(settings_path)
                print(f"[INFO] Added org.openclover pluginGroup to {settings_path}")
            else:
                print(
                    f"[INFO] org.openclover pluginGroup already exists in {settings_path}"
                )
        else:
            root = ET.Element("settings")
            root.set("xmlns", MAVEN_SETTINGS_NAMESPACE)
            root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
            root.set(
                "xsi:schemaLocation",
                f"{MAVEN_SETTINGS_NAMESPACE} http://maven.apache.org/xsd/settings-1.0.0.xsd",
            )
            pg = ET.SubElement(root, "pluginGroups")
            ET.SubElement(pg, "pluginGroup").text = "org.openclover"
            tree = ET.ElementTree(root)
            ET.register_namespace("", MAVEN_SETTINGS_NAMESPACE)
            tree.write(settings_path, encoding="utf-8", xml_declaration=True)
            _format_xml(settings_path)
            print(f"[INFO] Created {settings_path} with org.openclover pluginGroup")

        return True
    except Exception as e:
        print(f"[ERROR] Failed to configure Maven settings: {e}")
        return False


# =========================================================================
# JVM: top-level init
# =========================================================================


def _cleanup_rts_artifacts(project_path: str) -> None:
    """Clean up existing RTS artifacts from previous runs."""
    artifacts = {
        ".jcg",
        "diffLog",
        "classes-javacg_merged.jar-output_javacg",
    }
    for root, dirs, _ in os.walk(project_path):
        for d in dirs:
            if d in artifacts:
                p = os.path.join(root, d)
                try:
                    shutil.rmtree(p)
                    print(f"[INFO] Deleted: {p}")
                except OSError as e:
                    print(f"[WARNING] Failed to delete {p}: {e}")


def _configure_surefire_settings(project_path: str) -> None:
    """Configure surefire settings for RTS compatibility."""
    _delete_surefire_config_element(project_path, "reuseForks", "true")
    _delete_surefire_config_element(project_path, "forkCount", "1")


def _git_commit_changes(project_path: str, tool_name: str) -> bool:
    """Commit RTS configuration changes to git."""
    print("[INFO] Committing RTS configuration changes...")
    run_cmd(
        f"git config --global --add safe.directory {project_path}",
        cwd=project_path,
    )
    run_cmd('git config --global user.email "you@example.com"', cwd=project_path)
    run_cmd('git config --global user.name "Your Name"', cwd=project_path)

    if not os.path.exists(os.path.join(project_path, ".git")):
        if not run_cmd("git init", cwd=project_path):
            return False

    if not run_cmd("git add -A", cwd=project_path):
        return False

    msg = f"[RTS] Configure {tool_name} for regression test selection"
    if not run_cmd(f'git commit -m "{msg}" --allow-empty', cwd=project_path):
        return False

    print(f"[INFO] Changes committed: {msg}")
    return True


def init_jvm(project_path: str, tool_name: str) -> bool:
    """
    Initialize RTS for a JVM (Maven) project.

    Project-specific steps only -- Maven artifact installation is assumed
    to have happened in the Docker base image.
    """
    project_path = os.path.abspath(project_path)
    project_name = os.path.basename(project_path.rstrip("/"))

    if tool_name not in RTS_TOOLS:
        print(f"[ERROR] Unknown RTS tool: {tool_name}")
        return False

    pom_files = find_pom_files(project_path)
    if not pom_files:
        print("[ERROR] No pom.xml files found in project")
        return False

    print(f"[INFO] Found {len(pom_files)} pom.xml file(s)")

    _ensure_xmllint()

    # Step 1: Clean up stale artifacts
    print("[INFO] Step 1: Cleaning up existing RTS artifacts...")
    _cleanup_rts_artifacts(project_path)

    # Step 2: OpenClover-specific Maven settings
    if tool_name == "openclover":
        print("[INFO] Step 2: Configuring Maven settings for OpenClover...")
        if not _configure_maven_settings_for_openclover():
            print("[WARNING] Failed to configure Maven settings for OpenClover")

        print("[INFO] Step 2b: Adding OpenClover to pluginManagement...")
        for pom in pom_files:
            _add_openclover_to_plugin_management(pom)

    # Step 3: Add RTS plugins to all pom.xml files
    print("[INFO] Step 3: Adding RTS plugins to pom.xml files...")
    for pom in pom_files:
        if _add_rts_plugins_to_pom(pom, project_name, tool_name):
            print(f"[INFO] Modified: {pom}")
        else:
            print(f"[WARNING] Failed to modify: {pom}")

    # Step 4: Configure surefire settings
    print("[INFO] Step 4: Configuring surefire settings...")
    _configure_surefire_settings(project_path)

    # Step 5: Parse and add test patterns from test.sh
    print(f"[INFO] Step 5: Parsing test patterns from {TEST_SH_PATH}...")
    include_patterns, exclude_patterns = parse_test_patterns_from_test_sh(TEST_SH_PATH)

    if include_patterns:
        print(f"[INFO] Adding {len(include_patterns)} include pattern(s)...")
        for pom in pom_files:
            _add_patterns_to_surefire(pom, include_patterns, "includes", "include")

    if exclude_patterns:
        print(f"[INFO] Adding {len(exclude_patterns)} exclude pattern(s)...")
        for pom in pom_files:
            _add_patterns_to_surefire(pom, exclude_patterns, "excludes", "exclude")

    # OpenClover internal class exclusion
    if tool_name == "openclover":
        print("[INFO] Adding OpenClover internal class exclusion pattern...")
        for pom in pom_files:
            _add_patterns_to_surefire(
                pom, [OPENCLOVER_EXCLUDE_PATTERN], "excludes", "exclude"
            )
        print("[INFO] Adding OpenClover dependency to pom.xml files...")
        for pom in pom_files:
            _add_openclover_dependency_to_pom(pom)

    # Step 6: Commit changes
    print("[INFO] Step 6: Committing changes to git...")
    _git_commit_changes(project_path, tool_name)

    print("[INFO] JVM RTS initialization completed successfully!")
    return True


# =========================================================================
# C/C++: BinaryRTS project-specific init
# =========================================================================


def init_c(project_path: str, tool_name: str) -> bool:
    """
    Initialize RTS for a C/C++ project.

    Tools (/opt/pin, /opt/binary-rts, ctags) are assumed pre-installed in the
    base image. This function only performs project-specific configuration.

    Currently BinaryRTS does not require per-project init beyond verifying
    that the tools are present, but we keep this entry-point for future
    project-level config (e.g. custom ctags patterns, exclude lists).
    """
    project_path = os.path.abspath(project_path)
    print(f"[INFO] Initializing C/C++ RTS for: {project_path}")

    # Verify tool presence
    pin_root = "/opt/pin"
    binary_rts_dir = "/opt/binary-rts"
    missing = []
    if not os.path.isdir(pin_root):
        missing.append(f"Pin tool not found at {pin_root}")
    if not os.path.isdir(binary_rts_dir):
        missing.append(f"binary-rts not found at {binary_rts_dir}")
    try:
        subprocess.run(["ctags", "--version"], capture_output=True, timeout=10)
    except FileNotFoundError:
        missing.append("ctags not found in PATH")
    except Exception:
        pass

    if missing:
        for m in missing:
            print(f"[WARNING] {m}")
        print(
            "[WARNING] Some BinaryRTS dependencies are missing. "
            "They should be pre-installed in the base image."
        )

    print(
        "[INFO] C/C++ RTS project initialization completed (no project-specific steps required)."
    )
    return True


# =========================================================================
# Main
# =========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified RTS project initialization (JVM and C/C++)"
    )
    parser.add_argument(
        "project_path",
        help="Path to the project root",
    )
    parser.add_argument(
        "--tool",
        default=os.environ.get("RTS_TOOL", "jcgeks"),
        help="RTS tool name (default: jcgeks or RTS_TOOL env var)",
    )

    args = parser.parse_args()

    if not os.path.isdir(args.project_path):
        print(f"[ERROR] Project path does not exist: {args.project_path}")
        sys.exit(1)

    lang = os.environ.get("FUZZING_LANGUAGE", "").lower()
    print(f"[INFO] Detected FUZZING_LANGUAGE={lang!r}")

    if lang == "jvm":
        success = init_jvm(args.project_path, args.tool)
    elif lang in ("c", "c++"):
        success = init_c(args.project_path, args.tool)
    else:
        print(
            f"[WARNING] Unknown or unset FUZZING_LANGUAGE={lang!r}. "
            "Attempting JVM init as default."
        )
        success = init_jvm(args.project_path, args.tool)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
