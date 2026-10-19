"""
把一份（或一个目录下所有）政策文档导入 policy_documents / policy_chunks 表：
读正文 → 切 chunk → 调 Gemini Embedding API 批量生成向量 → 落库。

幂等设计：按 source_path（绝对路径）识别"是不是同一份文档"——重新导入同一个
文件时，先把这份文档名下所有旧 chunk 删掉，再重新切、重新生成向量插入，不是
在旧 chunk 基础上增量更新。这么做是因为"重新导入"通常意味着文档内容变了或者
切分逻辑/embedding 模型换了，旧 chunk 的顺序、数量、向量都可能跟新的对不上，
增量 diff 的复杂度不值得——政策文档数量不大（几十份），全量重切的成本很低。

用法（在 backend/ 目录、激活 venv 之后）：
    python -m app.services.policy_ingest --dir /path/to/你的政策文件夹
    python -m app.services.policy_ingest --file ~/Downloads/某份政策.pdf
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import PolicyChunk, PolicyDocument
from app.services.embeddings import embed_texts
from app.services.gemini_errors import GeminiCallError, GeminiConfigError
from app.services.policy_chunker import UnsupportedDocumentTypeError, chunk_document

SUPPORTED_SUFFIXES = {".docx", ".pdf"}


@dataclass
class IngestResult:
    documents_processed: int = 0
    chunks_created: int = 0
    skipped_files: list[str] = field(default_factory=list)  # 每项是 "文件名：原因"


async def ingest_document(db: Session, file_path: str, title: str | None = None) -> int:
    """导入单份文档，返回这份文档最终生成了多少个 chunk。不在这里 commit——
    交给调用方决定什么时候提交（导入目录时希望"一份文档失败不影响已经成功的
    其他文档"，所以每份文档各自提交，见 ingest_directory）。
    """
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")

    chunks = chunk_document(str(path))
    if not chunks:
        raise ValueError(f"没有从这份文档里提取到任何正文：{path}（可能是扫描版 PDF，没有可提取的文字层）")

    resolved_title = title or path.stem

    existing = db.scalar(select(PolicyDocument).where(PolicyDocument.source_path == str(path)))
    if existing is not None:
        # 幂等：旧 chunk 全部删掉重新生成，理由见模块文档。cascade 已经在模型里
        # 配置了 "all, delete-orphan"，直接清空 relationship 列表就会级联删除。
        existing.chunks.clear()
        existing.title = resolved_title
        document = existing
    else:
        document = PolicyDocument(title=resolved_title, source_path=str(path), doc_type=path.suffix.lstrip("."))
        db.add(document)
    db.flush()  # 需要拿到 document.id 才能建 chunk 的外键

    vectors = await embed_texts([c.content for c in chunks], task_type="RETRIEVAL_DOCUMENT")
    if len(vectors) != len(chunks):
        raise RuntimeError(
            f"Embedding 返回的向量数量（{len(vectors)}）跟 chunk 数量（{len(chunks)}）对不上，"
            f"这是 embeddings.py 内部的顺序保证被破坏了，属于代码 bug，不应该发生。"
        )

    for chunk, vector in zip(chunks, vectors):
        db.add(PolicyChunk(
            document_id=document.id, chunk_index=chunk.chunk_index,
            content=chunk.content, page_number=chunk.page_number, embedding=vector,
        ))

    return len(chunks)


async def ingest_directory(dir_path: str) -> IngestResult:
    root = Path(dir_path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"不是一个目录：{root}")

    result = IngestResult()
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES | {".doc"})

    db = SessionLocal()
    try:
        for file_path in files:
            if file_path.suffix.lower() not in SUPPORTED_SUFFIXES:
                result.skipped_files.append(
                    f"{file_path.name}：.doc 是旧的二进制格式，暂不支持直接读取，"
                    f"需要先用 Word/LibreOffice 另存为 .docx 再导入"
                )
                continue
            try:
                chunk_count = await ingest_document(db, str(file_path))
                db.commit()
                result.documents_processed += 1
                result.chunks_created += chunk_count
            except GeminiConfigError:
                # 没配 Key 这种配置问题，换下一份文件重试也一样会失败，没有必要
                # 把几十份文件的报错都刷一遍，直接原样往上抛，让用户先把配置修好。
                db.rollback()
                raise
            except (UnsupportedDocumentTypeError, ValueError, FileNotFoundError) as exc:
                # 这几种是已知的、可预期的"这份文件有问题"：格式不支持/没有可提取的
                # 正文/文件不存在。
                db.rollback()
                result.skipped_files.append(f"{file_path.name}：{exc}")
            except GeminiCallError as exc:
                # 跟"文件本身有问题"完全是两码事——是调用 Gemini Embedding API
                # 失败（网络问题，或者更常见的：免费层配额/限流，重试了几次之后
                # 还是没扛过去）。文案里必须跟下面"文件损坏"的情况分开说清楚，
                # 不然用户会误以为是文件坏了，去检查一份完全正常的文件（真实
                # 发生过一次：一份文件因为 429 配额限制被跳过，之前的报错文案
                # 却写的是"文件可能已损坏"，误导了排查方向）。
                db.rollback()
                result.skipped_files.append(
                    f"{file_path.name}：调用 Gemini Embedding API 失败（不是文件本身的问题，"
                    f"通常是免费层配额限流或网络问题），重试后仍未成功，稍后单独用 --file "
                    f"重新导入这一份即可：{exc}"
                )
            except Exception as exc:
                # 兜底：docx/pdf 解析库遇到损坏文件时抛出的异常类型五花八门（比如
                # python-docx 读取一份"看起来是 docx、实际 zip 结构损坏"的文件会
                # 抛 KeyError，不是上面几种"预期内"的异常类型）——真实撞到过这个
                # 情况（一份.docx文件内部zip结构损坏，读取时抛出
                # KeyError: "There is no item named 'NULL' in the archive"），
                # 如果不在这里兜底，一份文件损坏会让整个目录的导入直接中断，
                # 违背"一份文档失败不影响已经成功的其他文档"这条设计目标。
                db.rollback()
                result.skipped_files.append(
                    f"{file_path.name}：文件可能已损坏，读取时报错 "
                    f"{type(exc).__name__}: {exc}"
                )
    finally:
        db.close()

    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description="导入政策文档（.docx/.pdf）到向量检索库")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="单个文件路径")
    group.add_argument("--dir", help="目录路径，会递归导入其中所有 .docx/.pdf 文件")
    args = parser.parse_args()

    if args.file:
        db = SessionLocal()
        try:
            count = asyncio.run(ingest_document(db, args.file))
            db.commit()
            print(f"导入完成：{args.file} → {count} 个片段")
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    else:
        result = asyncio.run(ingest_directory(args.dir))
        print(f"共处理 {result.documents_processed} 份文档，生成 {result.chunks_created} 个片段。")
        if result.skipped_files:
            print("以下文件被跳过：")
            for s in result.skipped_files:
                print(f"  - {s}")


if __name__ == "__main__":
    _main()
