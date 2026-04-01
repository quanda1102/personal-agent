from openai import OpenAI
import mlx.core as mx
import numpy as np
from mlx_embeddings import load
from transformers import AutoTokenizer
from dotenv import load_dotenv

load_dotenv()

# ── OpenAI ───────────────────────────────────────────────────────────────────
openai_client = OpenAI()

def get_embeddings_openai(texts: list[str]) -> mx.array:
    resp = openai_client.embeddings.create(input=texts, model="text-embedding-3-large")
    vecs = [d.embedding for d in resp.data]
    arr = mx.array(vecs)
    return arr / mx.linalg.norm(arr, axis=-1, keepdims=True)

# ── Qwen3 loader ─────────────────────────────────────────────────────────────
def load_qwen(model_path: str):
    model, _ = load(model_path)
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    return model, tokenizer

def get_embeddings_qwen(texts: list[str], model, tokenizer) -> mx.array:
    encoded = tokenizer(texts, padding=True, truncation=True, return_tensors="np")
    input_ids = mx.array(encoded["input_ids"])
    attention_mask = mx.array(encoded["attention_mask"])
    output = model(input_ids, attention_mask=attention_mask)
    mask = attention_mask[..., None].astype(mx.float32)
    embeddings = (output.last_hidden_state * mask).sum(axis=1) / mask.sum(axis=1)
    return embeddings / mx.linalg.norm(embeddings, axis=-1, keepdims=True)

# ── Load models ───────────────────────────────────────────────────────────────
base = "/Users/quananhdang/.lmstudio/models/mlx-community"
print("Loading Qwen3-0.6B...")
qwen_06_model, qwen_06_tok = load_qwen(f"{base}/Qwen3-Embedding-0.6B-mxfp8")
print("Loading Qwen3-8B...")
qwen_8b_model, qwen_8b_tok = load_qwen(f"{base}/Qwen3-Embedding-8B-4bit-DWQ")
print("All models loaded!\n")

# ── Test texts ────────────────────────────────────────────────────────────────
texts = [
    "Tôi ngủ trước 11 giờ đêm hầu hết các ngày trong tuần",
    "Gần đây tôi hay thức khuya xem video, sáng dậy thấy mệt",
    "Tôi đặt báo thức 6 giờ sáng nhưng thường snooze 2-3 lần",
    "Mỗi tối tôi viết journal 10 phút trước khi đi ngủ",
    "Tôi uống cà phê buổi sáng để tỉnh táo hơn",
    "Sau bữa trưa tôi thường buồn ngủ và khó tập trung",
    "Tôi tập gym 3 buổi mỗi tuần vào buổi tối",
    "Hôm nay tôi bỏ buổi tập vì lười, cảm thấy tội lỗi",
    "Tôi dùng app theo dõi calories mỗi ngày sau bữa ăn",
    "Cuối tuần tôi thường phá vỡ thói quen ăn uống lành mạnh",
]
labels = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]

# ── Compute ───────────────────────────────────────────────────────────────────
print("Computing embeddings...")
openai_sim = mx.matmul(get_embeddings_openai(texts), get_embeddings_openai(texts).T)
qwen06_sim = mx.matmul(get_embeddings_qwen(texts, qwen_06_model, qwen_06_tok),
                       get_embeddings_qwen(texts, qwen_06_model, qwen_06_tok).T)
qwen8b_sim = mx.matmul(get_embeddings_qwen(texts, qwen_8b_model, qwen_8b_tok),
                       get_embeddings_qwen(texts, qwen_8b_model, qwen_8b_tok).T)

# ── Print matrix ──────────────────────────────────────────────────────────────
def print_matrix(name: str, matrix):
    print(f"\n{'─'*40}")
    print(f"  {name}")
    print(f"{'─'*40}")
    header = "      " + "  ".join(f"  {l}  " for l in labels)
    print(header)
    for i, row_label in enumerate(labels):
        row = f"  {row_label}  "
        for j in range(len(labels)):
            val = float(matrix[i, j])
            if i == j:
                row += "  --- "
            else:
                marker = "🔴" if val > 0.75 else "🟡" if val > 0.65 else "⚪"
                row += f" {marker}{val:.2f}"
        print(row)

print_matrix("OpenAI text-embedding-3-large", openai_sim)
print_matrix("Qwen3-0.6B (MLX direct)", qwen06_sim)
print_matrix("Qwen3-8B (MLX direct)", qwen8b_sim)

# ── Correlation with OpenAI as ground truth ───────────────────────────────────
def flatten_upper(matrix):
    n = matrix.shape[0]
    return [float(matrix[i, j]) for i in range(n) for j in range(i+1, n)]

openai_flat = flatten_upper(openai_sim)
qwen06_flat = flatten_upper(qwen06_sim)
qwen8b_flat = flatten_upper(qwen8b_sim)

corr_06 = np.corrcoef(openai_flat, qwen06_flat)[0, 1]
corr_8b = np.corrcoef(openai_flat, qwen8b_flat)[0, 1]

winner = "Qwen3-0.6B" if corr_06 > corr_8b else "Qwen3-8B"

print(f"\n{'─'*55}")
print("  Correlation with OpenAI 3-large (ground truth)")
print(f"{'─'*55}")
print(f"  {'Model':<20} {'Correlation':>12}  {'Rank':>6}")
print(f"  {'─'*51}")

ranked = sorted([("Qwen3-0.6B", corr_06), ("Qwen3-8B", corr_8b)], key=lambda x: x[1], reverse=True)
for rank, (name, corr) in enumerate(ranked, 1):
    crown = " 👑" if rank == 1 else ""
    print(f"  {name:<20} {corr:>12.4f}  {rank:>6}{crown}")

print(f"\n  → {winner} ranks more similarly to OpenAI")
print(f"  → Better choice for RAG on this domain")