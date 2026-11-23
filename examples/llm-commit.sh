#!/usr/local/bin/pysh
"""llm-commit - draft a commit message for your staged changes with a local LLM.

Reads `git diff --staged`, shows a local Ollama model the diff plus your recent commit subjects (so it
can match your style), and prints a suggested message. Nothing leaves your machine, and nothing is
committed unless you pass --commit, which opens your editor on the draft first.
"""
import argparse, os, re, sys, tempfile


def clean(s):
    """text from outside made safe to print: control characters (terminal escapes) become '?', newlines and tabs are kept"""
    return re.sub(r"[\x00-\x08\x0b-\x0c\x0d-\x1f\x7f-\x9f]", "?", str(s))

def staged(max_chars):
    if not !(git rev-parse --git-dir):
        sys.exit("llm-commit: not inside a git repository")
    stat = $(git diff --staged --stat)
    diff = $(git diff --staged --no-color)
    if not diff.strip(): sys.exit("llm-commit: nothing is staged (git add something first)")
    note = ""
    if len(diff) > max_chars:
        note = f"\n[diff truncated: {len(diff) - max_chars} more characters not shown]"
        diff = diff[:max_chars]
    recent = $(git log -8 --format=%s)
    return stat, diff + note, recent

def build_prompt(stat, diff, recent, style):
    rules = {"conventional": "Use the Conventional Commits format: type(scope): summary (feat, fix, docs, refactor, test, chore).",
             "plain": "Match the style of the recent commit subjects below."}[style]
    return f"""You write git commit messages. {rules}
Write a subject line of at most 72 characters in the imperative mood ("Add", "Fix"), then a blank line,
then a short body (at most 4 lines) explaining WHY if it is not obvious. Output only the message itself:
no code fences, no commentary.

Recent commit subjects in this repository:
{recent or '(none yet)'}

Files changed:
{stat}

Diff:
{diff}
"""

def tidy(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```\w*\n|\n```$", "", text).strip().strip('"')
    return text

def generate(prompt, model, host):
    try:
        import ollama
    except ImportError:
        sys.exit("llm-commit: the 'ollama' Python package is not installed")
    client = ollama.Client(host=host)
    if not model:
        try: models = [m.model for m in client.list().models]
        except Exception as e: sys.exit(f"llm-commit: cannot reach Ollama at {host}: {e}")
        if not models: sys.exit("llm-commit: Ollama has no models; try `ollama pull llama3.2`")
        model = models[0]
    print(f"model: {model}", file=sys.stderr)
    msgs = [{"role": "user", "content": prompt}]
    opts = {"temperature": 0.2, "num_predict": 400}
    try:
        try: r = client.chat(model=model, messages=msgs, options=opts, think=False)
        except TypeError: r = client.chat(model=model, messages=msgs, options=opts)     # older client without `think`
    except Exception as e:
        sys.exit(f"llm-commit: model request failed: {e}")
    return tidy(r.message.content or "")

def main(argv=None):
    ap = argparse.ArgumentParser(prog="llm-commit", description="Draft a commit message for the staged changes using a local Ollama model.",
                                 epilog="examples:  llm-commit   |   llm-commit --commit   |   llm-commit --model qwen3:4b --style conventional")
    ap.add_argument("-m", "--model", default=os.environ.get("OLLAMA_MODEL"), help="Ollama model (default: $OLLAMA_MODEL, else the first model installed)")
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434", help="Ollama server (default: $OLLAMA_HOST or %(default)s)")
    ap.add_argument("--style", choices=("plain", "conventional"), default="plain", help="message style (default: match recent commits)")
    ap.add_argument("--max-chars", type=int, default=12000, help="send at most this much of the diff to the model (default 12000)")
    ap.add_argument("--commit", action="store_true", help="open your editor on the draft and, if you save it, commit")
    ap.add_argument("--print-prompt", action="store_true", help="show the prompt that would be sent and exit (no model is contacted)")
    a = ap.parse_args(argv)
    if "://" not in a.host: a.host = "http://" + a.host

    stat, diff, recent = staged(a.max_chars)
    prompt = build_prompt(stat, diff, recent, a.style)
    if a.print_prompt:
        print(prompt); return 0
    msg = generate(prompt, a.model, a.host)
    if not msg: sys.exit("llm-commit: the model returned an empty message")
    msg = clean(msg)                                  # model output is untrusted text: no terminal escapes
    print(msg)
    if a.commit:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="llm-commit-", delete=False) as f: f.write(msg + "\n"); path = f.name
        try:
            r = $[git commit -e -F @(path)]                 # your editor opens on the draft; quit without saving to abort
            return r.returncode
        finally:
            os.unlink(path)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
