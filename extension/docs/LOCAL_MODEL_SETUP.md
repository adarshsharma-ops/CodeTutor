# Use CodeTutor with a local Ollama model

CodeTutor can use a model that runs on your computer. No API key is required and your
Python code stays local. Local model quality varies; Claude or OpenAI will usually give
stronger explanations and reasoning.

1. Install Ollama from <https://ollama.com/download> and open the Ollama application.
2. In a terminal, download the recommended model:

   ```bash
   ollama pull qwen2.5-coder:7b
   ```

3. Confirm Ollama is running:

   ```bash
   curl http://127.0.0.1:11434/api/tags
   ```

4. Return to VS Code and run **CodeTutor: Set up model provider**. Choose **No — use a
   local model**. CodeTutor will detect Ollama and the models installed on it.
5. Restart the mentor service so it reads the updated `.env`, then start CodeTutor.

If CodeTutor says Ollama is installed but not running, open **Ollama.app** and choose
**Check again**. A browser showing `404` at `/v1` is not itself a failure; CodeTutor
checks Ollama's supported model-list endpoint instead.
