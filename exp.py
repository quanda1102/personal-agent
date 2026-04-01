from mlx_audio.tts.generate import generate_audio
from mlx_audio.tts.utils import load_model
import subprocess

model = load_model("/Users/quananhdang/.lmstudio/models/mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit")

generate_audio(
    model=model,
    text="Hardware Recommendations At least 10GB of free disk space At least 16GB of RAM Nvidia GPU with a minimum of 4GB of VRAM By default, the model will utilize the GPU. In the absence of a GPU, it will run on the CPU and run much slower. Required Software Git Python version >=3.9 and <= 3.11. The default version is set to 3.11, but you can modify the Python version in the run.sh file.""",
    voice="af_heart",   # preset voice, không cần ref_audio
    speed=1.0,
    file_prefix="test_no_clone",
)

subprocess.run(["afplay", "test_no_clone_000.wav"])