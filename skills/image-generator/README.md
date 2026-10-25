# Image Generator Skill

The **Image Generator** skill gives agents two tools:

| Tool | Purpose |
| --- | --- |
| `generate_image` | Generate validated image artifacts through an enabled provider. |
| `list_providers` | List the providers that are currently available and enabled, with their capabilities. |

Providers: `google-gemini` (default), `openrouter`, `automatic1111` (local), `comfyui` (local), `mock`.

## Availability model

A provider is listed and usable when it is **registered** and its `<id>_enabled`
toggle is **on**. That toggle is the only gate — local providers need no extra
opt-in. `list_providers` reports each provider's `configured` flag and the
`missing_config` fields that still need an administrator value.

---

## ComfyUI provider

ComfyUI is a **local** provider: it submits an administrator-approved workflow
template to your own ComfyUI server, polls the job, and downloads the result.

### Prerequisites

1. A running ComfyUI server that is **reachable from the Evonic host**.
2. Every model / LoRA / custom node referenced by your workflow is already
   installed on that ComfyUI. The provider does not install or validate models.
3. (Optional) An API key if a reverse proxy in front of ComfyUI requires one.

### Step 1 — Export the workflow in **API format**

In ComfyUI:

1. **Settings → Enable Dev Mode Options**.
2. Open the workflow, then in the **Workflow** menu choose **Save (API Format)**.

The file must be the flat, node-id keyed JSON that looks like this:

```json
{
  "3": { "class_type": "KSampler", "inputs": { "seed": 0, "positive": ["6", 0], "latent_image": ["5", 0] } },
  "5": { "class_type": "EmptyLatentImage", "inputs": { "width": 1024, "height": 1024, "batch_size": 1 } },
  "6": { "class_type": "CLIPTextEncode", "inputs": { "text": "a prompt", "clip": ["11", 0] } }
}
```

> ⚠️ The normal **Save / Export** writes the *UI* format (top-level `nodes`,
> `links`, `definitions`). The provider rejects that format with
> `provider_configuration: ... has no prompt input to fill`. Always export with
> **Save (API Format)**.

### Step 2 — Install the workflow template

Place the exported file under the provider's workflow directory:

```
skills/image-generator/backend/providers/workflows/<name>.json
```

* `<name>` must equal the **Workflow Template** setting you configure in Step 3.
  Spaces are allowed (e.g. template `my workflow v2` → `my workflow v2.json`).
* The name is sanitised: path separators, `..` and leading dots are rejected.
* Workflow files are server-specific, so keep them out of version control:

  ```bash
  printf '%s\n' 'skills/image-generator/backend/providers/workflows/<name>.json' >> .git/info/exclude
  ```

### Step 3 — Configure the provider

Open **Settings → Skills → Image Generator**, pick **ComfyUI** in the provider
selector, and fill in:

| Field | Required | Description |
| --- | --- | --- |
| **Enabled** | yes | Turns the provider on. This is the only availability gate. |
| **ComfyUI Endpoint** | yes | Base URL of the ComfyUI API, e.g. `http://127.0.0.1:8188` (ComfyUI's default port) or `http://comfyui-host:8188`. HTTP and private hosts are allowed for local providers. It must be reachable **from the Evonic host**. |
| **ComfyUI Workflow Template** | no | Template name resolved to `providers/workflows/<name>.json`. Defaults to `default` (a bundled generic SDXL workflow). |
| **ComfyUI Job Timeout (seconds)** | no | Overall deadline for one job, `1`–`1800`. Default `600`. Raise it for slow models / busy queues. |
| **ComfyUI Polling Interval (seconds)** | no | How often the job history is checked, `1`–`10`. Default `2`. |
| **ComfyUI API Key** | no | If set, sent as `Authorization: Bearer <key>` on every request. |

Global settings that affect all providers:

| Field | Description |
| --- | --- |
| **Default Provider** | Provider used by `generate_image` when the call omits `provider`. Set it to `comfyui` to make ComfyUI the default. |

> After changing provider **code**, restart Evonic so the provider package is
> reloaded. Changing `skill.json` settings only needs a page refresh.

### How the provider injects your request

The adapter only writes the provider-neutral prompt, seed and size into your
approved graph — agent input never reaches arbitrary nodes.

| Input | Target selection |
| --- | --- |
| Prompt | Node id `268` if it has a literal `text`; else the text node linked from the sampler's `positive` input; else a string node titled **"User Prompt"** (or an unambiguous single string node). |
| Seed | Node id `306` if present; else the first `KSampler`/seed node. If the request omits a seed, one is generated per requested image. |
| Size | The first latent/empty node exposing `width`/`height`. |

Known ComfyUI quirks handled automatically:

* `SaveImageExtended` metadata saving is disabled (avoids `KeyError: 'workflow'`).
* Self-referencing `clip` inputs on the reference LoRA-loader chain are routed to
  the real `CLIPLoader` when that exact broken shape is present.

### Capabilities

* **Sizes**: `512x512`, `768x768`, `1024x1024`, `768x1024`, `768x1344`, `832x1248`, `1024x768`, `1344x768`, `1248x832`.
* **Output format**: advertises `png`; the format actually written is whatever your `SaveImage` node produces (png/jpeg/webp/gif are all accepted).
* **Images per request**: up to `4` (one submission per image).
* **Seed**: supported. **Negative prompt / style / transparency / reference images**: not supported.

### How it talks to ComfyUI

| Call | Purpose |
| --- | --- |
| `POST /prompt` with `{"prompt": <api workflow>, "client_id": <uuid>}` | Queue the job. |
| `GET /history/<prompt_id>` | Poll until the job completes (bounded by the job timeout). |
| `GET /view?filename=&subfolder=&type=` | Download the first output image. |

### Example: a Krea-2 Turbo template

1. Export the Krea-2 workflow with **Save (API Format)**.
2. Save it as `skills/image-generator/backend/providers/workflows/krea2-turbo v2.json`.
3. Configure ComfyUI: Endpoint `http://<comfyui-host>:8188`, Workflow Template `krea2-turbo v2`, Enabled on.
4. Make sure the referenced models/LoRAs exist on the ComfyUI server.
5. Call `generate_image(prompt="a serene mountain lake at dawn", provider="comfyui", size="1024x1024")`.

### Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Provider missing from `list_providers` | Its `<id>_enabled` toggle is off, or Evonic is still running old provider code — restart Evonic. |
| `provider_unavailable`: "could not be reached within the configured timeout" | The endpoint is wrong or not reachable from the Evonic host. Verify with `curl <endpoint>/system_stats` **on the Evonic host**. |
| `provider_unavailable`: "did not complete the job within the configured timeout" | The queue is long or the model is slow. Raise **Job Timeout** (up to 1800s). |
| `provider_configuration`: "workflow template is not available" | The JSON file is missing or its name does not match the **Workflow Template** setting. |
| `provider_configuration`: "has no prompt input to fill" | The workflow was exported in **UI format**. Re-export with **Save (API Format)**. |
| `generation_failed`: "ComfyUI failed to execute the workflow" | Check the ComfyUI console. Usually a missing model/LoRA/custom node. The `SaveImageExtended` metadata crash is handled automatically. |
| Artifact MIME/dimension errors | The workflow's `SaveImage` node must write png/jpeg/webp/gif within the size limits. |

### Security notes

* The endpoint must be an administrator-configured URL; agents cannot override it.
* Local providers may use HTTP and private addresses directly.
* Cloud providers (Google Gemini, OpenRouter) still require HTTPS and a publicly
  routable host.
