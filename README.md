# LLM Workflow Engine (LWE) Cloudflare Decision Provider plugin

Cloudflare Decision Provider plugin for [LLM Workflow Engine](https://github.com/llm-workflow-engine/llm-workflow-engine).

Access to Cloudflare's [Clef](https://developers.cloudflare.com/workers-ai/models/clef/),
[Clef-flash](https://developers.cloudflare.com/workers-ai/models/clef-flash/), and
[Clef-omni](https://developers.cloudflare.com/workers-ai/models/clef-omni/) decision models
through [Workers AI](https://developers.cloudflare.com/workers-ai/).

## Installation

### Export credentials

Create a [Workers AI API token](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)
and obtain your Cloudflare account ID, then export them:

```bash
export CLOUDFLARE_ACCOUNT_ID=<ACCOUNT_ID>
export CLOUDFLARE_API_TOKEN=<API_TOKEN>
```

### From packages

Install the latest version directly from GitHub with pip:

```bash
pip install git+https://github.com/llm-workflow-engine/lwe-plugin-decision-cloudflare
```

### From source (recommended for development)

```bash
git clone https://github.com/llm-workflow-engine/lwe-plugin-decision-cloudflare.git
cd lwe-plugin-decision-cloudflare
pip install -e .
```

## Configuration

Add a decision preset to `config.yaml` in your LWE profile:

```yaml
decisions:
  presets:
    clef-triage:
      provider: cloudflare
      model: clef-omni
      timeout: 30.0
      max_retries: 2
```

Select `clef`, `clef-flash`, or `clef-omni`. Fully qualified IDs such as
`@cf/cloudflare/clef-flash` are also accepted. The plugin derives the endpoint
and body selector from the model and validates its media capabilities.

Optional `options.account_id` overrides `CLOUDFLARE_ACCOUNT_ID`.
`api_key_env_var` selects a different token environment variable.
Credentials are resolved only when invoking the provider.

The plugin registers `cloudflare` under `lwe_decision_providers`. It does not
require `plugins.enabled` configuration or appear in the chat CLI.

## Usage

### Python

```python
from lwe.decisions import DecisionRequest, DecisionService, DecisionSettings, Noul

settings = DecisionSettings.from_preset("clef-triage")
service = DecisionService(settings)
result = service.decide(DecisionRequest(
    state="Checkout is unavailable.",
    questions={"urgent": Noul(instructions="Does this need urgent attention?")},
))
print(result.model_dump(mode="json"))
```

Alternatively construct `DecisionSettings(provider="cloudflare", model="clef")`
directly. `from_preset("clef-triage", profile="production")` selects a preset
from the `production` LWE profile; omitting `profile` uses `default`.

### Ansible

```yaml
- name: Classify a support request
  lwe_decide:
    preset: clef-triage
    state: "{{ support_request }}"
    questions:
      urgent:
        type: noul
        instructions: Does this require urgent attention?
  register: triage
```

Use `triage.answers.urgent.noul` in your workflow's threshold and fallback logic.
See the [LWE decision API documentation](https://github.com/llm-workflow-engine/llm-workflow-engine/blob/main/docs/decisions.rst)
for result types, retries, check mode, and workflow configuration.

## Multimodal requests

All three hosted models accept images. The hosted API's documented audio and
video input fields are supported by Clef-omni. Although the Clef/Clef-flash
weights also describe video capabilities, their hosted schemas currently expose
only images, so this plugin rejects their audio/video fields locally.

Each item in `images`, `audio`, or `videos` can be:

- An `http://` or `https://` URL. LWE downloads the content, follows redirects,
  checks the HTTP status, and base64-encodes it. Failed downloads raise an error.
- An absolute local `file://` URI, such as `file:///var/tmp/call.wav`. Files are
  read on the Python process or Ansible execution host. Missing or unreadable
  files raise a file-specific error. In Python, use `Path(...).resolve().as_uri()`.
- A base64 data URL, such as `data:image/png;base64,...`, or a raw base64 string.
  Supplied base64 is validated and used without re-encoding; raw base64's MIME
  type is inferred from its binary signature. For an unrecognized format,
  supply an explicit MIME type in a data URL.

Plain paths are not guessed: use `file://` to distinguish a file from raw base64.
Provider API credentials are not attached to media downloads.

```python
from pathlib import Path
from lwe.decisions import DecisionRequest, Noul

request = DecisionRequest(
    state="Review this equipment and its recorded sound.",
    images=["https://example.com/equipment.jpg"],
    audio=[Path("call.wav").resolve().as_uri()],
    questions={"fault": Noul(instructions="Does this equipment appear faulty?")},
)
result = service.decide(request)
```

The plugin supplies these limits to LWE's shared loader:

| Input | Formats | Maximum items | Decoded bytes per item | Combined decoded bytes |
|---|---|---:|---:|---:|
| Images | PNG, JPEG, WebP | 4 | 4 MiB | 8 MiB |
| Audio (Omni) | WAV, MP3 | 4 | 8 MiB | 16 MiB shared with video |
| Video (Omni) | MP4, WebM | 2 | 16 MiB | 16 MiB shared with audio |

Media count and decoded byte limits are checked locally, including while
downloading or reading files. Cloudflare enforces the total request-body limit
and validates actual media contents, image resolution (16 megapixels), and
Omni durations (300 seconds per audio clip and 60 seconds per video).

Cloudflare may truncate text state to fit remaining context. Consult the model
pages above for current constraints and token accounting.
