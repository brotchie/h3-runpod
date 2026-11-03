"""ComfyUI API graphs, built in code (no exported templates).

Each builder takes a params dict and returns an API-format prompt. Node ids
are readable names ("sampler", "cond", ...) so `gen.py run --set
sampler_select.sampler_name=euler` can override any input.

Builders (see WORKFLOWS at the bottom for defaults):
  h3          H3 Singularity video + audio (MP4). 0..9 reference images, cited in the
              prompt as <Picture 1>, <Picture 2>... sparse=1.3 by default; sparse=0 for
              dense hero shots; turbo=1 for 4-step motion drafts (colours run hot).

Speed option: attention="kitchen" (INT8 attention). torch.compile crashes on H3's kernels.
"""

H3 = {"unet": "Minimax-h3_Singularity_ref2va_v2_pruned_int8.safetensors",
      "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
      "vae": "minimax_h3_video_vae_int8_convrot.safetensors", "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
      "turbo": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"}


class Graph:
    def __init__(self):
        self.nodes = {}

    def add(self, nid, cls, **inputs):
        self.nodes[nid] = {"class_type": cls, "inputs": inputs, "_meta": {"title": nid}}
        return nid

    @staticmethod
    def out(nid, idx=0):
        return [nid, idx]


def _speedups(g, model, p):
    """Optional model patch: attention backend."""
    if p.get("attention") == "kitchen":
        model = g.add("attention", "ModelAttentionBackend", model=g.out(model), attention="comfy kitchen attention")
    return model


def build_h3(p):
    if len(p["refs"]) > 9:
        raise ValueError("h3 takes at most 9 --ref images")
    if p["compile"]:
        raise ValueError("h3 can't use compile: torch.compile crashes on its custom kernels")
    g = Graph()
    g.add("unet", "UNETLoader", unet_name=H3["unet"], weight_dtype="default")
    g.add("clip", "CLIPLoader", clip_name=H3["clip"], type="minimax", device="default")
    g.add("vae", "VAELoader", vae_name=H3["vae"])
    g.add("audio_vae", "VAELoader", vae_name=H3["audio_vae"])
    model = "unet"
    if p["turbo"]:
        model = g.add("turbo", "LoraLoaderModelOnly", model=g.out(model), lora_name=H3["turbo"], strength_model=p["turbo"])
    model = _speedups(g, model, p)
    if p["sparse"]:
        model = g.add("sparse", "BlockSparseAttention", model=g.out(model), selection="sol-attn",
                      **{"selection.tau": p["sparse"]}, start_percent=p["sparse_start"], end_percent=1.0,
                      dense_blocks="", min_tokens=12288, extra_tokens=256,
                      sink_conditioning="exact_kv_and_rows", verbose=False)
    refs = {f"ref_images.ref_image_{i}": g.out(g.add(f"ref{i + 1}", "LoadImage", image=name))
            for i, name in enumerate(p["refs"])}
    for i, name in enumerate(p["audios"]):
        refs[f"ref_audios.ref_audio_{i}"] = g.out(g.add(f"audio{i + 1}", "LoadAudio", audio=name))
    g.add("cond", "MiniMaxH3ReferenceToVideo", clip=g.out("clip"), vae=g.out("vae"), audio_vae=g.out("audio_vae"),
          prompt=p["prompt"], width=p["width"], height=p["height"], length=p["length"], ref_image_size="match", **refs)
    g.add("noise", "RandomNoise", noise_seed=p["seed"])
    g.add("sampler_select", "KSamplerSelect", sampler_name=p["sampler"])
    g.add("sigmas", "BasicScheduler", model=g.out(model), scheduler=p["scheduler"],
          steps=4 if p["turbo"] else p["steps"], denoise=1.0)
    g.add("guider", "BasicGuider", model=g.out(model), conditioning=g.out("cond"))
    g.add("sampler", "SamplerCustomAdvanced", noise=g.out("noise"), guider=g.out("guider"), sampler=g.out("sampler_select"),
          sigmas=g.out("sigmas"), latent_image=g.out("cond", 1))
    g.add("decode", "VAEDecode", samples=g.out("sampler"), vae=g.out("vae"))
    g.add("decode_audio", "VAEDecodeAudio", samples=g.out("sampler"), vae=g.out("audio_vae"))
    g.add("video", "CreateVideo", images=g.out("decode"), audio=g.out("decode_audio"), fps=24)
    g.add("save", "SaveVideo", video=g.out("video"), filename_prefix=p["prefix"], format="auto", codec="auto")
    return g.nodes


COMMON = {"prompt": "", "negative": "", "refs": [], "width": 1664, "height": 928, "seed": 0, "batch": 1,
          "prefix": "h3/x", "attention": "pytorch", "compile": False}
WORKFLOWS = {
    # length: 17k+5 frames at 24 fps (124 = ~5s). sparse attention measured 1.57x faster than dense on an H100
    "h3": (build_h3, {**COMMON, "width": 1344, "height": 768, "length": 124, "steps": 20, "sampler": "res_multistep",
                      "scheduler": "simple", "sparse": 1.3, "sparse_start": 0.2, "turbo": 0.0, "audios": []}),
}


def build(name, **params):
    fn, defaults = WORKFLOWS[name]
    unknown = set(params) - set(defaults)
    if unknown:
        raise ValueError(f"{name} doesn't take {sorted(unknown)}; takes {sorted(defaults)}")
    return fn({**defaults, **params})
