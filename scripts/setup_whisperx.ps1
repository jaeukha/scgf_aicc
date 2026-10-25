param([string]$Python = ".\.venv\Scripts\python.exe")

# WhisperX pins PyTorch 2.8; restore the CUDA and Decider-compatible packages after its dependency install.
& $Python -m pip install whisperx==3.8.6
& $Python -m pip install --force-reinstall --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0
& $Python -m pip install --force-reinstall --no-deps transformers==5.17.0 tokenizers==0.23.3 huggingface-hub==1.31.0
