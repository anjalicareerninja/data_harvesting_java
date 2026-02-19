"""
Run solutions from a JSONL file and write one CSV row per question.
Output: 14 columns — question_id, question, s1_solution, s2_solution, s3_solution,
        then for each solution: output, runtime_s, space_kb.
Usage: python run_eval.py <input.jsonl> [--out results.csv]
"""
import csv
import json
import sys
import time
from pathlib import Path

# run from data/ so local imports work
sys.path.insert(0, str(Path(__file__).resolve().parent))

from code import CodeStore
from code_splicer import CodeSplicer
from executor import LanguageExecutor

TIMEOUT = 5


def run_one(record, code_store, code_splicer, executor):
    lang = record["lang"]
    func_code = record["func_code"]
    main_code = record.get("main_code", "")
    if "\\n" in main_code and "\n" not in main_code:
        main_code = main_code.replace("\\n", "\n")
    question_id = record.get("question_id", "?")
    solution_id = record.get("solution_id", "?")

    src_uid = f"{question_id}_{solution_id}"
    spliced = code_splicer.splice_code(lang, func_code, main_code)["spliced_code"]
    request_data = {"src_uid": src_uid, "lang": lang, "source_code": spliced}

    language_config = code_store.build_code_env(request_data)
    try:
        if language_config.get("syntax_error"):
            print(f"[{question_id}_{solution_id}] error: Syntax error in code", file=sys.stderr)
            return question_id, solution_id, "Syntax error in code", 0.0, 0

        start = time.time()
        result = executor.execute(language_config, TIMEOUT)
        runtime = round(time.time() - start, 3)
        outcome = result.get("outcome", "UNKNOWN")
        space_kb = result.get("process_peak_memory", 0) or 0

        # Actual output: per-test results or full error (no verdict-only)
        output = (
            result.get("exec_test_output")
            or result.get("stderr", "").strip()
            or result.get("stdout", "").strip()
            or result.get("exec_runtime_message", "").strip()
            or outcome
        )
        if isinstance(output, str) and len(output) > 8000:
            output = output[:8000] + "\n... (truncated)"

        if outcome != "PASSED":
            print(f"[{question_id}_{solution_id}] error:\n{output}", file=sys.stderr)
        return question_id, solution_id, output, runtime, space_kb
    finally:
        code_store.destroy_code_env(language_config)


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_eval.py <input.jsonl> [--out results.csv]", file=sys.stderr)
        sys.exit(1)

    input_path = Path(sys.argv[1])
    out_path = Path("results.csv")
    if len(sys.argv) >= 4 and sys.argv[2] == "--out":
        out_path = Path(sys.argv[3])

    code_store = CodeStore()
    code_splicer = CodeSplicer()
    executor = LanguageExecutor()

    rows_by_question = {}
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            qid, sid, outcome, runtime, space = run_one(rec, code_store, code_splicer, executor)
            func_code = rec.get("func_code", "")
            if qid not in rows_by_question:
                main_code = rec.get("main_code", "")
                if "\\n" in main_code and "\n" not in main_code:
                    main_code = main_code.replace("\\n", "\n")
                rows_by_question[qid] = {"_question": rec.get("question", ""), "_main_code": main_code}
            rows_by_question[qid][sid] = (outcome, runtime, space, func_code)

    # question_id, question, full_test_func, then s1/s2/s3 solution + metrics
    headers = ["question_id", "question", "full_test_func",
               "s1_solution", "s1_output", "s1_runtime_s", "s1_space_kb",
               "s2_solution", "s2_output", "s2_runtime_s", "s2_space_kb",
               "s3_solution", "s3_output", "s3_runtime_s", "s3_space_kb"]

    def _sort_key(qid):
        s = str(qid)
        if s.isdigit():
            return (0, int(s))
        return (1, s)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for qid in sorted(rows_by_question.keys(), key=_sort_key):
            row = rows_by_question[qid]
            q_text = row.get("_question", "")
            main_code = row.get("_main_code", "")
            s1 = row.get("s1", ("", 0, 0, ""))
            s2 = row.get("s2", ("", 0, 0, ""))
            s3 = row.get("s3", ("", 0, 0, ""))
            w.writerow([qid, q_text, main_code,
                        s1[3], s1[0], s1[1], s1[2],
                        s2[3], s2[0], s2[1], s2[2],
                        s3[3], s3[0], s3[1], s3[2]])

    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
