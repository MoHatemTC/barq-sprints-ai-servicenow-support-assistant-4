#!/usr/bin/env python3
"""Semantic search demo / evidence tool.

    python cli/search_pdf.py "what happens after two failed escalations" --top-k 5
    python cli/search_pdf.py "ما هي خطوات تصعيد الحادث" --type diagram
    python cli/search_pdf.py --inspect          # dump payloads of pdf points (no embedding call)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from ingest_pdf import env  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--type", help="filter content_type: text|table|image_ocr|diagram")
    ap.add_argument("--all-sources", action="store_true", help="do not restrict to source_type=pdf")
    ap.add_argument("--inspect", action="store_true", help="scroll pdf points and print payload (no vectors)")
    ap.add_argument("--full", action="store_true", help="print full chunk text (default truncates to 400/120 chars)")
    ap.add_argument("--collection", default=None,
                    help="Qdrant collection (default: QDRANT_COLLECTION/QDRANT_COLLECTION_NAME from env). "
                         "Use a scratch collection for tests; never the shared KB collection.")
    ap.add_argument("--doc-id", default=None,
                    help="Restrict results to one document id "
                         "(e.g. prove which document a hit comes from in a mixed collection).")
    ap.add_argument("--rerank", action="store_true",
                    help="LLM listwise rerank of the top-k (uses RERANK_MODEL or VISION_MODEL from env). "
                         "Prints RETRIEVED order, RERANKED order, and a TRUE/FALSE verdict.")
    ap.add_argument("--expect-page", type=int, default=None,
                    help="Ground-truth page number: with --rerank, verdict TRUE means this page is #1.")
    ap.add_argument("--expect-type", default=None,
                    help="Ground-truth content_type (text|table|image_ocr|diagram); checked together with --expect-page.")
    ap.add_argument("--env-file", default=".env")
    a = ap.parse_args()

    from dotenv import load_dotenv
    from qdrant_client import QdrantClient
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    load_dotenv(a.env_file)
    coll = a.collection or env("QDRANT_COLLECTION", "QDRANT_COLLECTION_NAME")
    qc = QdrantClient(url=env("QDRANT_URL"), api_key=env("QDRANT_API_KEY"))

    must = [] if a.all_sources else [FieldCondition(key="source_type", match=MatchValue(value="pdf"))]
    if a.doc_id:
        must.append(FieldCondition(key="doc_id", match=MatchValue(value=a.doc_id)))
    if a.type:
        must.append(FieldCondition(key="content_type", match=MatchValue(value=a.type)))
    flt = Filter(must=must) if must else None

    if a.inspect:
        pts, _ = qc.scroll(coll, scroll_filter=flt, limit=a.top_k, with_payload=True, with_vectors=False)
        for p in pts:
            pl = {k: v for k, v in p.payload.items() if k not in ("text", "table_json")}
            t = p.payload.get("text", "")
            pl["text"] = t if a.full else (t[:120] + "...")
            print(json.dumps({"id": str(p.id), "payload": pl}, ensure_ascii=False, indent=2))
        return
    if not a.query:
        ap.error("query required")

    from openai import OpenAI
    oc = OpenAI(base_url=env("LITELLM_BASE_URL", "LITELLM_API_BASE", "OPENAI_BASE_URL"),
                api_key=env("LITELLM_API_KEY", "LITELLM_MASTER_KEY", "OPENAI_API_KEY"))
    kw = {"dimensions": int(env("EMBEDDING_DIM", "EMBED_DIM"))} if env("EMBEDDING_DIM", "EMBED_DIM") else {}
    vec = oc.embeddings.create(model=env("EMBEDDING_MODEL", "EMBED_MODEL"), input=[a.query], **kw).data[0].embedding
    vname = env("QDRANT_VECTOR_NAME")
    res = qc.query_points(coll, query=vec, using=vname, query_filter=flt, limit=a.top_k, with_payload=True).points
    print(f"\nquery: {a.query}\n")
    print("--- RETRIEVED (dense, Qdrant order) ---")
    for i, r in enumerate(res, 1):
        p = r.payload
        print(f"#{i} score={r.score:.3f} source_type={p.get('source_type')} type={p.get('content_type')} "
              f"file={p.get('source_filename')} page={p.get('page_number')} (range={p.get('page_range')}) chunk={p.get('chunk_index')}")
        t = p.get("text", "").replace("\n", "\n   ")
        print("   " + (t if a.full else t[:400]) + "\n")

    def _matches(r) -> bool:
        p = r.payload
        if a.expect_page is not None:
            pno = p.get("page_number")
            prange = p.get("page_range", "")
            matched = (pno == a.expect_page)
            if not matched and prange:
                parts = str(prange).split("-")
                try:
                    if len(parts) == 1 and int(parts[0]) == a.expect_page:
                        matched = True
                    elif len(parts) == 2 and int(parts[0]) <= a.expect_page <= int(parts[1]):
                        matched = True
                except ValueError:
                    pass
            if not matched:
                return False
        if a.expect_type and p.get("content_type") != a.expect_type and p.get("content_type") != "text":
            return False
        return True

    def _verdict(items) -> str | None:
        if a.expect_page is None:
            return None
        return "TRUE" if items and _matches(items[0]) else "FALSE"

    v0 = _verdict(res)
    if v0 is not None and res:
        print(f"RETRIEVED verdict (dense #1 == expected p{a.expect_page}"
              f"{'/' + a.expect_type if a.expect_type else ''}): {v0}")
        exp = [(i, r) for i, r in enumerate(res, 1) if _matches(r)]
        if exp:
            rank, hit = exp[0]
            gap = res[0].score - hit.score
            print(f"TRUE hit: rank=#{rank} dense={hit.score:.3f} "
                  f"p{hit.payload.get('page_number')} {hit.payload.get('content_type')} "
                  f"chunk={hit.payload.get('chunk_index')} (gap to #1: {gap:.3f})")
            above = [(i, r) for i, r in enumerate(res, 1) if i < rank]
            if above:
                print(f"DISTRACTORS scoring higher than TRUE ({len(above)}):")
                for i, r in above:
                    print(f"  #{i} dense={r.score:.3f} (+{r.score - hit.score:.3f} over TRUE) "
                          f"p{r.payload.get('page_number')} {r.payload.get('content_type')} "
                          f"chunk={r.payload.get('chunk_index')}")
            else:
                print("DISTRACTORS scoring higher than TRUE: none — TRUE is #1")
        else:
            print(f"TRUE hit: NOT in top-{len(res)} — expected p{a.expect_page}"
                  f"{'/' + a.expect_type if a.expect_type else ''} missing entirely")

    if a.rerank and res:
        rmodel = env("RERANK_MODEL", "VISION_MODEL", "LITELLM_VISION_MODEL")
        if not rmodel:
            print("RERANKED: FAIL no rerank model (set RERANK_MODEL or VISION_MODEL in .env)")
            return
        docs = "\n".join(
            f"[{i}] (p{r.payload.get('page_number')} {r.payload.get('content_type')}) "
            f"{(r.payload.get('text', '')[:600]).replace(chr(10), ' ')}"
            for i, r in enumerate(res, 1))
        prompt = ("Rank these passages by relevance to the query. "
                  "Reply with JSON only: {\"order\": [n, ...]} listing ALL numbers best-first.\n\n"
                  f"Query: {a.query}\n\nPassages:\n{docs}")
        try:
            rr = oc.chat.completions.create(model=rmodel, temperature=0, max_tokens=200,
                                            messages=[{"role": "user", "content": prompt}])
            import re as _re
            m = _re.search(r"\{.*\}", rr.choices[0].message.content or "", _re.S)
            order = json.loads(m.group())["order"] if m else []
            order = [n for n in order if isinstance(n, int) and 1 <= n <= len(res)]
            order += [i for i in range(1, len(res) + 1) if i not in order]
        except Exception as e:
            print(f"RERANKED: FAIL rerank call failed ({type(e).__name__})")
            return
        rres = [res[n - 1] for n in order]
        print("--- RERANKED (LLM order) ---")
        for i, r in enumerate(rres, 1):
            p = r.payload
            print(f"#{i} was=#{order[i - 1]} dense={r.score:.3f} type={p.get('content_type')} "
                  f"file={p.get('source_filename')} page={p.get('page_number')} chunk={p.get('chunk_index')}")
        v1 = _verdict(rres)
        if v1 is not None:
            print(f"RERANKED verdict (reranked #1 == expected p{a.expect_page}"
                  f"{'/' + a.expect_type if a.expect_type else ''}): {v1}")
            exp = [(i, r) for i, r in enumerate(rres, 1) if _matches(r)]
            if exp:
                rank, hit = exp[0]
                print(f"TRUE hit after rerank: rank=#{rank} (was=#{order[rank - 1]} dense={hit.score:.3f})")
                above = [(i, r) for i, r in enumerate(rres, 1) if i < rank]
                if above:
                    print(f"DISTRACTORS still above TRUE after rerank ({len(above)}):")
                    for i, r in above:
                        print(f"  #{i} was=#{order[i - 1]} dense={r.score:.3f} "
                              f"p{r.payload.get('page_number')} {r.payload.get('content_type')} "
                              f"chunk={r.payload.get('chunk_index')}")
                else:
                    print("DISTRACTORS above TRUE after rerank: none — TRUE is #1")
        else:
            top = rres[0].payload
            print(f"RERANKED truth: #1 is p{top.get('page_number')} {top.get('content_type')} "
                  f"chunk={top.get('chunk_index')}")


if __name__ == "__main__":
    main()
