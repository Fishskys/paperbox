# Paperbox's derived docling-serve image: the release image ships the layout,
# table-former and OCR models but **not** the formula model, so
# `do_formula_enrichment=true` fails with
#   Preset 'default' not found for CodeFormulaVlmOptions
#   Model 'docling-project/CodeFormulaV2' not found in artifacts_path
# Baking it in keeps the runtime container offline (it has no egress: its DNS
# resolves huggingface.co to IPv6, which the container network cannot route).
#
# Build (the download needs the WSL host proxy, hence --network=host; the image
# is built locally and never pulled, so compose only references the tag):
#
#   cd infra && docker build --network=host \
#     --build-arg HTTPS_PROXY=http://127.0.0.1:7890 \
#     --build-arg HTTP_PROXY=http://127.0.0.1:7890 \
#     -t paperbox-docling-cpu:v1.35.0-formula docling
#
# Rebuild this image whenever BASE_IMAGE moves to a new docling version.
FROM quay.io/docling-project/docling-serve-cpu:v1.35.0

ARG HTTPS_PROXY
ARG HTTP_PROXY

# Runs as the image's own user (1001), which already owns the artifacts dir:
# /opt/app-root/src/.cache/docling/models — also where docling looks for models,
# so no --output-dir and no extra runtime config are needed.
RUN HTTPS_PROXY="$HTTPS_PROXY" HTTP_PROXY="$HTTP_PROXY" \
    docling-tools models download-hf-repo docling-project/CodeFormulaV2

# Fail the build instead of shipping an image that 404s on formulas.
RUN test -f /opt/app-root/src/.cache/docling/models/docling-project--CodeFormulaV2/model.safetensors \
    && du -sh /opt/app-root/src/.cache/docling/models
