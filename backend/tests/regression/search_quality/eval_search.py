import argparse
import builtins
import json
from contextlib import contextmanager
from typing import Any
from typing import TextIO

from sqlalchemy.orm import Session

from danswer.db.engine import get_sqlalchemy_engine
from danswer.direct_qa.qa_utils import get_chunks_for_qa
from danswer.document_index.factory import get_default_document_index
from danswer.indexing.models import InferenceChunk
from danswer.search.models import IndexFilters
from danswer.search.models import RerankMetricsContainer
from danswer.search.models import RetrievalMetricsContainer
from danswer.search.search_runner import danswer_search
from danswer.server.models import QuestionRequest
from danswer.utils.callbacks import MetricsHander


engine = get_sqlalchemy_engine()


@contextmanager
def redirect_print_to_file(file: TextIO) -> Any:
    original_print = builtins.print
    builtins.print = lambda *args, **kwargs: original_print(*args, file=file, **kwargs)
    try:
        yield
    finally:
        builtins.print = original_print


def read_json(file_path: str) -> dict:
    with open(file_path, "r") as file:
        return json.load(file)


def word_wrap(s: str, max_line_size: int = 100, prepend_tab: bool = True) -> str:
    words = s.split()

    current_line: list[str] = []
    result_lines: list[str] = []
    current_length = 0
    for word in words:
        if len(word) > max_line_size:
            if current_line:
                result_lines.append(" ".join(current_line))
                current_line = []
                current_length = 0

            result_lines.append(word)
            continue

        if current_length + len(word) + len(current_line) > max_line_size:
            result_lines.append(" ".join(current_line))
            current_line = []
            current_length = 0

        current_line.append(word)
        current_length += len(word)

    if current_line:
        result_lines.append(" ".join(current_line))

    return "\t" + "\n\t".join(result_lines) if prepend_tab else "\n".join(result_lines)


def get_search_results(
    query: str, db_session: Session
) -> tuple[
    list[InferenceChunk],
    RetrievalMetricsContainer | None,
    RerankMetricsContainer | None,
]:
    filters = IndexFilters(
        source_type=None,
        document_set=None,
        time_cutoff=None,
        access_control_list=None,
    )
    question = QuestionRequest(
        query=query,
        filters=filters,
        enable_auto_detect_filters=False,
    )

    retrieval_metrics = MetricsHander[RetrievalMetricsContainer]()
    rerank_metrics = MetricsHander[RerankMetricsContainer]()

    top_chunks, llm_chunk_selection, query_id = danswer_search(
        question=question,
        user=None,
        db_session=db_session,
        document_index=get_default_document_index(),
        bypass_acl=True,
        retrieval_metrics_callback=retrieval_metrics.record_metric,
        rerank_metrics_callback=rerank_metrics.record_metric,
    )

    llm_chunks_indices = get_chunks_for_qa(
        chunks=top_chunks,
        llm_chunk_selection=llm_chunk_selection,
        token_limit=None,
    )

    llm_chunks = [top_chunks[i] for i in llm_chunks_indices]

    return (
        llm_chunks,
        retrieval_metrics.metrics,
        rerank_metrics.metrics,
    )


def _print_retrieval_metrics(
    metrics_container: RetrievalMetricsContainer, show_all: bool = False
) -> None:
    for ind, metric in enumerate(metrics_container.metrics):
        if not show_all and ind >= 10:
            break

        if ind != 0:
            print()  # for spacing purposes
        print(f"\tDocument: {metric.document_id}")
        section_start = metric.chunk_content_start.replace("\n", " ")
        print(f"\tSection Start: {section_start}")
        print(f"\tSimilarity Distance Metric: {metric.score}")


def _print_reranking_metrics(
    metrics_container: RerankMetricsContainer, show_all: bool = False
) -> None:
    # Printing the raw scores as they're more informational than post-norm/boosting
    for ind, metric in enumerate(metrics_container.metrics):
        if not show_all and ind >= 10:
            break

        if ind != 0:
            print()  # for spacing purposes
        print(f"\tDocument: {metric.document_id}")
        section_start = metric.chunk_content_start.replace("\n", " ")
        print(f"\tSection Start: {section_start}")
        print(f"\tSimilarity Score: {metrics_container.raw_similarity_scores[ind]}")


def calculate_score(
    log_prefix: str, chunk_ids: list[str], targets: list[str], max_chunks: int = 5
) -> float:
    top_ids = chunk_ids[:max_chunks]
    matches = [top_id for top_id in top_ids if top_id in targets]
    print(f"{log_prefix} Hits: {len(matches)}/{len(targets)}", end="\t")
    return len(matches) / min(len(targets), max_chunks)


def main(
    questions_json: str, output_file: str, show_details: bool, stop_after: int
) -> None:
    questions_info = read_json(questions_json)

    running_retrieval_score = 0.0
    running_rerank_score = 0.0
    running_llm_filter_score = 0.0

    with open(output_file, "w") as outfile:
        with redirect_print_to_file(outfile):
            print("Running Document Retrieval Test\n")

            with Session(engine, expire_on_commit=False) as db_session:
                for ind, (question, targets) in enumerate(questions_info.items()):
                    if ind >= stop_after:
                        break

                    print(f"\n\nQuestion: {question}")

                    (
                        top_chunks,
                        retrieval_metrics,
                        rerank_metrics,
                    ) = get_search_results(query=question, db_session=db_session)

                    assert retrieval_metrics is not None and rerank_metrics is not None

                    retrieval_ids = [
                        metric.document_id for metric in retrieval_metrics.metrics
                    ]
                    retrieval_score = calculate_score(
                        "Retrieval", retrieval_ids, targets
                    )
                    running_retrieval_score += retrieval_score
                    print(f"Average: {running_retrieval_score / (ind + 1)}")

                    rerank_ids = [
                        metric.document_id for metric in rerank_metrics.metrics
                    ]
                    rerank_score = calculate_score("Rerank", rerank_ids, targets)
                    running_rerank_score += rerank_score
                    print(f"Average: {running_rerank_score / (ind + 1)}")

                    llm_ids = [chunk.document_id for chunk in top_chunks]
                    llm_score = calculate_score("LLM Filter", llm_ids, targets)
                    running_llm_filter_score += llm_score
                    print(f"Average: {running_llm_filter_score / (ind + 1)}")

                    if show_details:
                        print("\nRetrieval Metrics:")
                        if retrieval_metrics is None:
                            print("No Retrieval Metrics Available")
                        else:
                            _print_retrieval_metrics(retrieval_metrics)

                        print("\nReranking Metrics:")
                        if rerank_metrics is None:
                            print("No Reranking Metrics Available")
                        else:
                            _print_reranking_metrics(rerank_metrics)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "regression_questions_json",
        type=str,
        help="Path to the Questions JSON file.",
        default="./tests/regression/search_quality/test_questions.json",
        nargs="?",
    )
    parser.add_argument(
        "--output_file",
        type=str,
        help="Path to the output results file.",
        default="./tests/regression/search_quality/regression_results.txt",
    )
    parser.add_argument(
        "--show_details",
        action="store_true",
        help="If set, show details of the retrieved chunks.",
        default=True,
    )
    parser.add_argument(
        "--stop_after",
        type=int,
        help="Stop processing after this many iterations.",
        default=10,
    )
    args = parser.parse_args()

    main(
        args.regression_questions_json,
        args.output_file,
        args.show_details,
        args.stop_after,
    )
