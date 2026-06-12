"""Static technology detector.

Scans all imports in the dependency graph and maps them to known external
systems, frameworks, and infrastructure components — with zero LLM calls.

The output feeds every AI prompt so diagrams are grounded in real signal
rather than inference from file names alone.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import networkx as nx


# ---------------------------------------------------------------------------
# Library → (system_name, category) mapping
# ---------------------------------------------------------------------------

_LIB_MAP: dict[str, tuple[str, str]] = {
    # ── Relational databases ───────────────────────────────────────────────
    "psycopg2":       ("PostgreSQL",      "Relational Database"),
    "asyncpg":        ("PostgreSQL",      "Relational Database"),
    "pg8000":         ("PostgreSQL",      "Relational Database"),
    "pymysql":        ("MySQL",           "Relational Database"),
    "MySQLdb":        ("MySQL",           "Relational Database"),
    "aiomysql":       ("MySQL",           "Relational Database"),
    "cx_Oracle":      ("Oracle DB",       "Relational Database"),
    "pyodbc":         ("SQL Server/ODBC", "Relational Database"),
    "sqlite3":        ("SQLite",          "Relational Database"),
    "sqlalchemy":     ("SQL Database",    "Relational Database"),
    "databases":      ("SQL Database",    "Relational Database"),
    "tortoise":       ("SQL Database",    "Relational Database"),
    "peewee":         ("SQL Database",    "Relational Database"),
    "django.db":      ("Django ORM",      "Relational Database"),
    # ── Document / NoSQL ──────────────────────────────────────────────────
    "pymongo":        ("MongoDB",         "Document Database"),
    "motor":          ("MongoDB",         "Document Database"),
    "mongoengine":    ("MongoDB",         "Document Database"),
    "elasticsearch":  ("Elasticsearch",   "Search Engine"),
    "opensearch":     ("OpenSearch",      "Search Engine"),
    "cassandra":      ("Cassandra",       "Wide-column Database"),
    "couchdb":        ("CouchDB",         "Document Database"),
    "firebase_admin": ("Firebase",        "Document Database"),
    # ── Cache / Message broker ────────────────────────────────────────────
    "redis":          ("Redis",           "Cache / Message Broker"),
    "aioredis":       ("Redis",           "Cache / Message Broker"),
    "memcache":       ("Memcached",       "Cache"),
    "pylibmc":        ("Memcached",       "Cache"),
    # ── Message queues ────────────────────────────────────────────────────
    "pika":           ("RabbitMQ",        "Message Queue"),
    "aio_pika":       ("RabbitMQ",        "Message Queue"),
    "kafka":          ("Kafka",           "Message Queue"),
    "confluent_kafka":("Kafka",           "Message Queue"),
    "celery":         ("Celery",          "Task Queue"),
    "dramatiq":       ("Dramatiq",        "Task Queue"),
    "rq":             ("Redis Queue",     "Task Queue"),
    "kombu":          ("Kombu",           "Message Queue"),
    # ── Cloud ─────────────────────────────────────────────────────────────
    "boto3":          ("AWS",             "Cloud Platform"),
    "botocore":       ("AWS",             "Cloud Platform"),
    "google.cloud":   ("Google Cloud",    "Cloud Platform"),
    "googleapiclient":("Google APIs",     "Cloud Platform"),
    "azure":          ("Azure",           "Cloud Platform"),
    "msrest":         ("Azure",           "Cloud Platform"),
    # ── HTTP / External APIs ──────────────────────────────────────────────
    "requests":       ("External HTTP API", "HTTP Client"),
    "httpx":          ("External HTTP API", "HTTP Client"),
    "aiohttp":        ("External HTTP API", "HTTP Client"),
    "urllib3":        ("External HTTP API", "HTTP Client"),
    "grpc":           ("gRPC Service",    "RPC"),
    "zerorpc":        ("ZeroRPC Service", "RPC"),
    # ── Auth / Identity ───────────────────────────────────────────────────
    "ldap3":          ("LDAP / Active Directory", "Authentication"),
    "python_ldap":    ("LDAP / Active Directory", "Authentication"),
    "jwt":            ("JWT Auth",        "Authentication"),
    "jose":           ("JWT Auth",        "Authentication"),
    "authlib":        ("OAuth Provider",  "Authentication"),
    "oauthlib":       ("OAuth Provider",  "Authentication"),
    "keycloak":       ("Keycloak",        "Authentication"),
    # ── Email ─────────────────────────────────────────────────────────────
    "smtplib":        ("SMTP Server",     "Email"),
    "sendgrid":       ("SendGrid",        "Email Service"),
    "mailgun":        ("Mailgun",         "Email Service"),
    "ses":            ("AWS SES",         "Email Service"),
    # ── Storage ───────────────────────────────────────────────────────────
    "minio":          ("MinIO / S3",      "Object Storage"),
    "paramiko":       ("SSH / SFTP",      "File Transfer"),
    "ftplib":         ("FTP Server",      "File Transfer"),
    # ── Monitoring / Logging ──────────────────────────────────────────────
    "sentry_sdk":     ("Sentry",          "Error Monitoring"),
    "datadog":        ("Datadog",         "Monitoring"),
    "prometheus_client":("Prometheus",    "Monitoring"),
    "opentelemetry":  ("OpenTelemetry",   "Observability"),
    # ── Web frameworks ────────────────────────────────────────────────────
    "flask":          ("Flask",           "Web Framework"),
    "fastapi":        ("FastAPI",         "Web Framework"),
    "django":         ("Django",          "Web Framework"),
    "tornado":        ("Tornado",         "Web Framework"),
    "starlette":      ("Starlette",       "Web Framework"),
    "sanic":          ("Sanic",           "Web Framework"),
    "falcon":         ("Falcon",          "Web Framework"),
    "bottle":         ("Bottle",          "Web Framework"),
    "aiohttp.web":    ("aiohttp",         "Web Framework"),
    "quart":          ("Quart",           "Web Framework"),
    # ── Data / ML ─────────────────────────────────────────────────────────
    "pandas":         ("Pandas",          "Data Processing"),
    "numpy":          ("NumPy",           "Data Processing"),
    "scipy":          ("SciPy",           "Data Processing"),
    "sklearn":        ("scikit-learn",    "ML Framework"),
    "tensorflow":     ("TensorFlow",      "ML Framework"),
    "torch":          ("PyTorch",         "ML Framework"),
    "transformers":   ("HuggingFace",     "ML Framework"),
    "pyspark":        ("Apache Spark",    "Big Data"),
    "dask":           ("Dask",            "Big Data"),
    "airflow":        ("Apache Airflow",  "Workflow Orchestrator"),
    "prefect":        ("Prefect",         "Workflow Orchestrator"),
    "luigi":          ("Luigi",           "Workflow Orchestrator"),
}

# Framework detection: if these imports appear, classify as this framework
_FRAMEWORK_PRIORITY = [
    "django", "fastapi", "flask", "tornado", "starlette",
    "sanic", "falcon", "bottle", "quart",
]

# Modules that are clearly internal and should not be flagged as external
_STDLIB_PREFIXES = {
    "os", "sys", "re", "io", "abc", "ast", "csv", "json", "math",
    "time", "random", "string", "typing", "pathlib", "datetime",
    "collections", "itertools", "functools", "contextlib", "dataclasses",
    "threading", "multiprocessing", "concurrent", "asyncio", "logging",
    "unittest", "http", "email", "urllib", "html", "xml", "socket",
    "struct", "hashlib", "hmac", "base64", "copy", "pprint", "enum",
    "uuid", "warnings", "traceback", "inspect", "importlib", "pkgutil",
    "subprocess", "shutil", "tempfile", "glob", "fnmatch", "stat",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TechContext:
    """All technology signal extracted from the dependency graph."""
    frameworks: list[str] = field(default_factory=list)
    external_systems: list[tuple[str, str]] = field(default_factory=list)
    # e.g. [("PostgreSQL", "Relational Database"), ("Redis", "Cache")]
    primary_languages: list[str] = field(default_factory=list)
    has_web_layer: bool = False
    has_database: bool = False
    has_queue: bool = False
    has_auth: bool = False
    has_cloud: bool = False

    def external_systems_text(self) -> str:
        if not self.external_systems:
            return "none detected"
        return "; ".join(f"{name} ({cat})" for name, cat in self.external_systems)

    def frameworks_text(self) -> str:
        return ", ".join(self.frameworks) if self.frameworks else "none detected"

    def summary(self) -> str:
        parts = []
        if self.frameworks:
            parts.append(f"Frameworks: {self.frameworks_text()}")
        if self.external_systems:
            parts.append(f"External systems: {self.external_systems_text()}")
        if self.primary_languages:
            parts.append(f"Languages: {', '.join(self.primary_languages)}")
        return " | ".join(parts) or "No technology context detected"


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

def detect_tech_context(graph: nx.DiGraph) -> TechContext:
    """Scan all imports in the graph and return a TechContext."""
    all_imports: set[str] = set()
    lang_counts: dict[str, int] = {}

    for _node, data in graph.nodes(data=True):
        for imp in data.get("imports", []):
            # Normalise: strip relative path prefixes, take root module
            root = _root_module(imp)
            if root and root not in _STDLIB_PREFIXES:
                all_imports.add(root)
                all_imports.add(imp)  # also check full import for sub-packages
        lang = data.get("language")
        if lang:
            lang_counts[lang] = lang_counts.get(lang, 0) + 1

    # Match imports against known lib map
    found: dict[str, tuple[str, str]] = {}  # system_name → (name, category)
    for imp in all_imports:
        imp_lower = imp.lower().replace("-", "_")
        for lib_key, (sys_name, category) in _LIB_MAP.items():
            if imp_lower == lib_key or imp_lower.startswith(lib_key + "."):
                found[sys_name] = (sys_name, category)
                break

    external = list(found.values())
    categories = {cat for _, cat in external}

    # Detect primary framework
    frameworks: list[str] = []
    for fw in _FRAMEWORK_PRIORITY:
        if any(imp.lower().startswith(fw) for imp in all_imports):
            fw_name = _LIB_MAP.get(fw, (fw.title(), ""))[0]
            if fw_name not in frameworks:
                frameworks.append(fw_name)

    # Top languages by file count
    primary_langs = [lang for lang, _ in
                     sorted(lang_counts.items(), key=lambda x: -x[1])[:3]]

    return TechContext(
        frameworks=frameworks,
        external_systems=external,
        primary_languages=primary_langs,
        has_web_layer=bool(frameworks) or "Web Framework" in categories,
        has_database="Relational Database" in categories or "Document Database" in categories,
        has_queue="Message Queue" in categories or "Task Queue" in categories,
        has_auth="Authentication" in categories,
        has_cloud="Cloud Platform" in categories,
    )


def _root_module(import_path: str) -> Optional[str]:
    """Extract root module name from an import path like 'src/auth/manager'."""
    # Handle Python-style dotted imports and path-style imports
    if "/" in import_path:
        return None  # internal path reference, skip
    parts = import_path.split(".")
    root = parts[0].replace("-", "_").lower()
    return root if root else None
