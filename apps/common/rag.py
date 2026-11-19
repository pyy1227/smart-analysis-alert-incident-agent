import os
from functools import lru_cache

from langchain_core.documents import Document
from langchain_text_splitters import CharacterTextSplitter

from apps.common.config import settings


def split_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    return CharacterTextSplitter(
        separator="\n", chunk_size=chunk_size, chunk_overlap=overlap,
    ).split_text(text)


@lru_cache(maxsize=1)
def _vectorstore():
    # 延迟导入，未使用 RAG 时不加载较重的依赖。
    import chromadb
    from langchain_chroma import Chroma
    from langchain_community.embeddings import DashScopeEmbeddings

    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY，无法生成 RAG 向量")

    client = chromadb.PersistentClient(path=settings.chroma_path)
    return Chroma(
        client=client,
        collection_name="knowledge",
        embedding_function=DashScopeEmbeddings(
            model=os.getenv("DASHSCOPE_EMBEDDING_MODEL", "text-embedding-v1"),
            dashscope_api_key=api_key,
        ),
    )


def add_document(
    tenant_id: str,
    service_id: str,
    source: str,
    content: str,
    version: str = "1",
):
    store = _vectorstore()
    docs = [
        Document(
            page_content=chunk,
            metadata={
                "tenant_id": tenant_id,
                "service_id": service_id,
                "source": source,
                "page_or_section": f"chunk-{index}",
                "document_version": version,
            },
            id=f"{tenant_id}:{source}:{version}:{index}",
        )
        for index, chunk in enumerate(split_text(content))
    ]
    if docs:
        store.add_documents(docs)


def retrieve(
    tenant_id: str,
    service_id: str,
    query: str,
    k: int = 5,
) -> list[dict]:
    """个人项目按租户和服务检索全部 Runbook。"""
    store = _vectorstore()
    where = {
        "$and": [
            {"tenant_id": {"$eq": tenant_id}},
            {"service_id": {"$eq": service_id}},
        ]
    }
    results = store.similarity_search_with_score(query=query, k=k, filter=where)
    return [{
        "id": doc.id,
        "content": doc.page_content,
        "metadata": doc.metadata,
        "score": score,
    } for doc, score in results]
