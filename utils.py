import torch
from torch import Tensor
from comfy.ldm.flux.layers import timestep_embedding


def has_affordable_memory(device: torch.device) -> bool:
    free_memory, _ = torch.cuda.mem_get_info(device)
    free_memory_gb = free_memory / (1024**3)
    return free_memory_gb > 24


def is_newer_than_ada_lovelace(device: torch.device) -> int:
    cc_major, cc_minor = torch.cuda.get_device_capability(device)
    return cc_major * 10 + cc_minor >= 89


teacache_coef = [
    4.98651651e02,
    -2.83781631e02,
    5.58554382e01,
    -3.82021401e00,
    2.64230861e-01,
]


def poly1d(x):
    result = torch.zeros_like(x)
    for i, coeff in enumerate(teacache_coef):
        result += coeff * (x ** (len(teacache_coef) - 1 - i))
    return result


def are_tensors_similar(tensor1: Tensor, tensor2: Tensor, threshold: float):
    assert tensor1.shape == tensor2.shape
    diff_abs_mean = (tensor1 - tensor2).abs().mean()
    tensor1_abs_mean = tensor1.abs().mean()
    diff = diff_abs_mean / tensor1_abs_mean
    return diff.item() < threshold


def skip_forward_orig(
    self,
    img: Tensor,
    img_ids: Tensor,
    txt: Tensor,
    txt_ids: Tensor,
    timesteps: Tensor,
    y: Tensor,
    guidance: Tensor = None,
    control=None,
    transformer_options={},
    attn_mask: Tensor = None,
) -> Tensor:
    patches_replace = transformer_options.get("patches_replace", {})
    ds_skip_blocks = transformer_options.get("ds_skip_blocks", [])
    ss_skip_blocks = transformer_options.get("ss_skip_blocks", [])

    if img.ndim != 3 or txt.ndim != 3:
        raise ValueError("Input img and txt tensors must have 3 dimensions.")

    # running on sequences img
    img = self.img_in(img)
    vec = self.time_in(timestep_embedding(timesteps, 256).to(img.dtype))
    if self.params.guidance_embed:
        if guidance is None:
            raise ValueError(
                "Didn't get guidance strength for guidance distilled model."
            )
        vec = vec + self.guidance_in(timestep_embedding(guidance, 256).to(img.dtype))

    vec = vec + self.vector_in(y[:, : self.params.vec_in_dim])
    txt = self.txt_in(txt)

    ids = torch.cat((txt_ids, img_ids), dim=1)
    pe = self.pe_embedder(ids)

    blocks_replace = patches_replace.get("dit", {})
    for i, block in enumerate(self.double_blocks):
        #### skip blocks
        if i in ds_skip_blocks:
            continue

        if ("double_block", i) in blocks_replace:

            def block_wrap(args):
                out = {}
                out["img"], out["txt"] = block(
                    img=args["img"],
                    txt=args["txt"],
                    vec=args["vec"],
                    pe=args["pe"],
                    attn_mask=args.get("attn_mask"),
                )
                return out

            out = blocks_replace[("double_block", i)](
                {"img": img, "txt": txt, "vec": vec, "pe": pe, "attn_mask": attn_mask},
                {"original_block": block_wrap},
            )
            txt = out["txt"]
            img = out["img"]
        else:
            img, txt = block(img=img, txt=txt, vec=vec, pe=pe, attn_mask=attn_mask)

        if control is not None:  # Controlnet
            control_i = control.get("input")
            if i < len(control_i):
                add = control_i[i]
                if add is not None:
                    img += add

    img = torch.cat((txt, img), 1)

    for i, block in enumerate(self.single_blocks):
        if ("single_block", i) in blocks_replace:
            #### skip blocks
            if i in ss_skip_blocks:
                continue

            def block_wrap(args):
                out = {}
                out["img"] = block(
                    args["img"],
                    vec=args["vec"],
                    pe=args["pe"],
                    attn_mask=args.get("attn_mask"),
                )
                return out

            out = blocks_replace[("single_block", i)](
                {"img": img, "vec": vec, "pe": pe, "attn_mask": attn_mask},
                {"original_block": block_wrap},
            )
            img = out["img"]
        else:
            img = block(img, vec=vec, pe=pe, attn_mask=attn_mask)

        if control is not None:  # Controlnet
            control_o = control.get("output")
            if i < len(control_o):
                add = control_o[i]
                if add is not None:
                    img[:, txt.shape[1] :, ...] += add

    img = img[:, txt.shape[1] :, ...]

    img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)
    return img


def teacache_skip_forward_orig(
    self,
    img: Tensor,
    img_ids: Tensor,
    txt: Tensor,
    txt_ids: Tensor,
    timesteps: Tensor,
    y: Tensor,
    guidance: Tensor = None,
    control=None,
    transformer_options={},
    attn_mask: Tensor = None,
) -> Tensor:
    patches_replace = transformer_options.get("patches_replace", {})
    rel_l1_threshold = transformer_options.get("rel_l1_threshold")
    ds_skip_blocks = transformer_options.get("ds_skip_blocks")
    ss_skip_blocks = transformer_options.get("ss_skip_blocks")

    if img.ndim != 3 or txt.ndim != 3:
        raise ValueError("Input img and txt tensors must have 3 dimensions.")

    # running on sequences img
    img = self.img_in(img)
    vec = self.time_in(timestep_embedding(timesteps, 256).to(img.dtype))
    if self.params.guidance_embed:
        if guidance is None:
            raise ValueError(
                "Didn't get guidance strength for guidance distilled model."
            )
        vec = vec + self.guidance_in(timestep_embedding(guidance, 256).to(img.dtype))

    vec = vec + self.vector_in(y[:, : self.params.vec_in_dim])
    txt = self.txt_in(txt)

    ids = torch.cat((txt_ids, img_ids), dim=1)
    pe = self.pe_embedder(ids)

    blocks_replace = patches_replace.get("dit", {})

    ### TeaCache
    img_, vec_ = img.clone(), vec.clone()
    img_mod1, _ = self.double_blocks[0].img_mod(vec_)
    curr_modulated_img = self.double_blocks[0].img_norm1(img_)
    curr_modulated_img = (1 + img_mod1.scale) * curr_modulated_img + img_mod1.shift

    try:
        self.accum_rel_l1_distance += poly1d(
            (curr_modulated_img - self.prev_modulated_img).abs().mean()
            / self.prev_modulated_img.abs().mean()
        )
    except:
        torch._dynamo.graph_break()  ### speeding up compiling
        self.accum_rel_l1_distance = rel_l1_threshold

    self.prev_modulated_img = curr_modulated_img

    if self.accum_rel_l1_distance < rel_l1_threshold:
        img += self.previous_residual
    else:
        self.accum_rel_l1_distance = 0
        orig_img = img.clone()

        for i, block in enumerate(self.double_blocks):
            ### skip blocks
            if i in ds_skip_blocks:
                continue

            if ("double_block", i) in blocks_replace:

                def block_wrap(args):
                    out = {}
                    out["img"], out["txt"] = block(
                        img=args["img"],
                        txt=args["txt"],
                        vec=args["vec"],
                        pe=args["pe"],
                        attn_mask=args.get("attn_mask"),
                    )
                    return out

                out = blocks_replace[("double_block", i)](
                    {
                        "img": img,
                        "txt": txt,
                        "vec": vec,
                        "pe": pe,
                        "attn_mask": attn_mask,
                    },
                    {"original_block": block_wrap},
                )
                txt = out["txt"]
                img = out["img"]
            else:
                img, txt = block(img=img, txt=txt, vec=vec, pe=pe, attn_mask=attn_mask)

            if control is not None:  # Controlnet
                control_i = control.get("input")
                if i < len(control_i):
                    add = control_i[i]
                    if add is not None:
                        img += add

        img = torch.cat((txt, img), 1)

        for i, block in enumerate(self.single_blocks):
            if ("single_block", i) in blocks_replace:
                ### skip blocks
                if i in ss_skip_blocks:
                    continue

                def block_wrap(args):
                    out = {}
                    out["img"] = block(
                        args["img"],
                        vec=args["vec"],
                        pe=args["pe"],
                        attn_mask=args.get("attn_mask"),
                    )
                    return out

                out = blocks_replace[("single_block", i)](
                    {"img": img, "vec": vec, "pe": pe, "attn_mask": attn_mask},
                    {"original_block": block_wrap},
                )
                img = out["img"]
            else:
                img = block(img, vec=vec, pe=pe, attn_mask=attn_mask)

            if control is not None:  # Controlnet
                control_o = control.get("output")
                if i < len(control_o):
                    add = control_o[i]
                    if add is not None:
                        img[:, txt.shape[1] :, ...] += add

        img = img[:, txt.shape[1] :, ...]

        self.previous_residual = img - orig_img

    img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)
    return img


def fbcache_skip_forward_orig(
    self,
    img: Tensor,
    img_ids: Tensor,
    txt: Tensor,
    txt_ids: Tensor,
    timesteps: Tensor,
    y: Tensor,
    guidance: Tensor = None,
    control=None,
    transformer_options={},
    attn_mask: Tensor = None,
) -> Tensor:
    patches_replace = transformer_options.get("patches_replace", {})
    fbcache_threshold = transformer_options.get("fbcache_threshold")
    validate_use_cached = transformer_options.get("validator")
    ds_skip_blocks = transformer_options.get("ds_skip_blocks")
    ss_skip_blocks = transformer_options.get("ss_skip_blocks")

    if img.ndim != 3 or txt.ndim != 3:
        raise ValueError("Input img and txt tensors must have 3 dimensions.")

    # running on sequences img
    img = self.img_in(img)
    vec = self.time_in(timestep_embedding(timesteps, 256).to(img.dtype))
    if self.params.guidance_embed:
        if guidance is None:
            raise ValueError(
                "Didn't get guidance strength for guidance distilled model."
            )
        vec = vec + self.guidance_in(timestep_embedding(guidance, 256).to(img.dtype))

    vec = vec + self.vector_in(y[:, : self.params.vec_in_dim])
    txt = self.txt_in(txt)

    ids = torch.cat((txt_ids, img_ids), dim=1)
    pe = self.pe_embedder(ids)

    blocks_replace = patches_replace.get("dit", {})

    for i, block in enumerate(self.double_blocks):
        #### skip blocks
        if i in ds_skip_blocks:
            if i == 0:
                use_cached = False
            continue

        if ("double_block", i) in blocks_replace:

            def block_wrap(args):
                out = {}
                out["img"], out["txt"] = block(
                    img=args["img"],
                    txt=args["txt"],
                    vec=args["vec"],
                    pe=args["pe"],
                    attn_mask=args.get("attn_mask"),
                )
                return out

            out = blocks_replace[("double_block", i)](
                {"img": img, "txt": txt, "vec": vec, "pe": pe, "attn_mask": attn_mask},
                {"original_block": block_wrap},
            )
            txt = out["txt"]
            img = out["img"]
        else:
            img, txt = block(img=img, txt=txt, vec=vec, pe=pe, attn_mask=attn_mask)

        if control is not None:  # Controlnet
            control_i = control.get("input")
            if i < len(control_i):
                add = control_i[i]
                if add is not None:
                    img += add

        ### FBCache
        if i == 0:
            try:
                use_cached = are_tensors_similar(
                    img, self.prev_first_block_output, fbcache_threshold
                )
                use_cached = validate_use_cached(use_cached, timesteps.item())
            except:
                torch._dynamo.graph_break()  ### speeding up compiling
                use_cached = False
            self.prev_first_block_output = img

            if use_cached:
                break

    if use_cached:
        img += self.prev_residual
    else:
        img = torch.cat((txt, img), 1)

        for i, block in enumerate(self.single_blocks):
            if ("single_block", i) in blocks_replace:
                #### skip blocks
                if i in ss_skip_blocks:
                    continue

                def block_wrap(args):
                    out = {}
                    out["img"] = block(
                        args["img"],
                        vec=args["vec"],
                        pe=args["pe"],
                        attn_mask=args.get("attn_mask"),
                    )
                    return out

                out = blocks_replace[("single_block", i)](
                    {"img": img, "vec": vec, "pe": pe, "attn_mask": attn_mask},
                    {"original_block": block_wrap},
                )
                img = out["img"]
            else:
                img = block(img, vec=vec, pe=pe, attn_mask=attn_mask)

            if control is not None:  # Controlnet
                control_o = control.get("output")
                if i < len(control_o):
                    add = control_o[i]
                    if add is not None:
                        img[:, txt.shape[1] :, ...] += add

        img = img[:, txt.shape[1] :, ...]

        self.prev_residual = img - self.prev_first_block_output

    img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)
    return img
