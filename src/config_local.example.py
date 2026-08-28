r"""Local machine overrides for src/config.py — copy me to config_local.py.

    copy src\config_local.example.py  src\config_local.py

Then edit config_local.py with the real paths for YOUR machine. That file is
gitignored, so your paths never get committed. Any ALL_CAPS variable you
define here replaces the value in src/config.py (same for any other tunable:
sampling params, stop rules, even SYSTEM_PROMPT).

Minimum you need for a first run — a multimodal setup (main GGUF + mmproj
vision encoder) served by llama.cpp's llama-server:

    JUDGE_SERVER_EXE = r"C:\llama\llama-server.exe"          # Windows
    # JUDGE_SERVER_EXE = "/opt/llama/llama-server"           # Linux/macOS
    LLAMA_ARG_MODEL = r"C:\models\Qwen3-VL-8B-Q5_K_M.gguf"   # any Qwen3-VL-family GGUF
    LLAMA_ARG_MMPROJ = r"C:\models\mmproj-Qwen3-VL-8B-f16.gguf"

Optional — the judge's task (default is generic image quality; this is how
you'd rank logos instead):

    # SYSTEM_PROMPT = (
    #     "You are an image judge. You will see two images, one after the other.\n"
    #     "The first image is A, the second image is B.\n"
    #     "Please respond with exactly one character: 'A' or 'B', and nothing else.\n\n"
    #     "Context: judge these logo designs on shape, mood and memorability."
    # )
"""
