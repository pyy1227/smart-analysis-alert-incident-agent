from pathlib import Path

from apps.common.rag import add_document

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "data" / "knowledge"

MAPPING = {
    "payment-timeout.md": "payment-api",
    "db-pool-exhausted.md": "payment-api",
    "prometheus-restart.md": "prometheus",
}

def main():
    for filename, service_id in MAPPING.items():
        path = KNOWLEDGE_DIR / filename
        if not path.exists():
            print(f"skip missing: {filename}")
            continue
        add_document(
            tenant_id="demo",
            service_id=service_id,
            source=f"runbook/{filename}",
            content=path.read_text(encoding="utf-8"),
            version="2026-09",
        )
        print(f"seeded: {filename} -> {service_id}")


if __name__ == "__main__":
    main()
