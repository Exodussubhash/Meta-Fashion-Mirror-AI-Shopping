# FashionMirror AI

A fashion storefront that finds real Shopify products and automatically shows personalized previews beside the original product photos. Each page displays up to 10 products.

## Tech stack

![Next.js](https://img.shields.io/badge/Next.js-000000?style=for-the-badge&logo=nextdotjs&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-06B6D4?style=for-the-badge&logo=tailwindcss&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Shopify MCP](https://img.shields.io/badge/Shopify_MCP-7AB55C?style=for-the-badge&logo=shopify&logoColor=white)
![Muse Spark 1.3](https://img.shields.io/badge/Muse_Spark_1.3-0866FF?style=for-the-badge&logo=meta&logoColor=white)
![Muse Image 1.0](https://img.shields.io/badge/Muse_Image_1.0-8B5CF6?style=for-the-badge&logo=meta&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Vercel](https://img.shields.io/badge/Vercel-000000?style=for-the-badge&logo=vercel&logoColor=white)

**Models:**
- `muse-spark-1.3`: conversational search, photo suitability assessment and classification of garments with missing category metadata.
- `muse-image-1.0`: hosted image editing using the uploaded photo and product image. No model is trained locally.

## Architecture

```text
Next.js storefront
  |
  +--- Search bar --------------------------+
  |    Direct catalogue query               |
  |                                        v
  +--- Chat ---> Muse Spark 1.3 ------> FastAPI search pipeline
  |              Query + optional           |
  |              price / size / colour      +--- Shopify Global Catalog MCP
  |              filters + chat context          search_catalog / lookup_catalog / get_product
  |                                        |
  |<--- Up to 10 products + cursor ----------+
  |     Filters retained during pagination
  |
  +--- Photo + chosen department ---> Upload validation / normalization
  |                                        |
  |                                  Muse Spark photo assessment
  |                                  Reused for same photo in session
  |
  +--- Displayed page ---> Batch: up to 10 product IDs
  |                            |
  |                       Preview eligibility checks
  |                       Department / garment / visible body areas
  |                       Missing category? Muse Spark classification
  |                       (during product search, cached per session)
  |                            |
  |                       Async job queue
  |                       3 generations concurrently across users
  |                       Recheck eligibility + photo before generation
  |                       100 attempts per session / rolling hour
  |                            |
  |                       Muse Image 1.0
  |                       User photo + selected product image
  |                            |
  |<--- Poll job status -------+--- Generated-image URL / failure / skip
  |     Per-product loading, zoom and manual retry
  |
  +--- Like / dislike / reason ---> feedback.csv
                                   Timestamp + job/product IDs + URLs

Provider calls / API requests / jobs ---> evaluation.csv ---> Report
Session tokens ---> Ownership checks on private session routes
Sessions / chat / products / jobs / quotas ---> In-memory state (one worker)
Uploaded photos / feedback / metrics ---> Private data directory
```

### Planned deployment

```text
Browser ---> Vercel (Next.js frontend)
    |
    +------> HTTPS backend host (FastAPI Docker container, one worker)
                  |
                  +--- Shopify Global Catalog MCP
                  +--- Meta APIs: Muse Spark / Muse Image
                  +--- Persistent volume: photos and CSV files
```

API keys stay in the backend. Session tokens scope access to previews and feedback; uploaded photos have no public serving route. Per-session quotas are implemented. Invite-only access, session-creation/API rate limits and a global usage cap remain deployment hardening tasks. The diagram describes the intended hosting layout; it does not claim the app is already deployed.

## Run locally

Use Node.js 20+ and Python 3.10+.

From the project root, install dependencies:

```sh
cp .env.example .env
cp frontend/.env.example frontend/.env.local
npm install --prefix frontend
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
```

Edit the root `.env`:

```dotenv
CATALOG_MODE=live
MODEL_API_KEY=your_meta_api_key
TRY_ON_PROVIDER=muse-image
```

The backend automatically loads `.env`. Local development has a default Shopify agent profile; production requires your own public `UCP_AGENT_PROFILE_URL`. Your Meta account needs access to both models. Use `TRY_ON_PROVIDER=none` to disable paid image generation, or `CATALOG_MODE=mock` for explicitly labelled sample products and simple mock chat.

**Terminal 1 — backend**, starting from the project root:

```sh
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload
```

**Terminal 2 — frontend**, starting from the project root:

```sh
npm run dev --prefix frontend
```

Storefront: http://localhost:3000  
API documentation: http://localhost:8000/docs

## Use the app

1. Select the wearer's clothing department and upload a photo.
2. Search or use chat; follow-up messages retain search context.
3. Eligible products on the displayed page automatically enter the preview queue.
4. Click a generated image to enlarge it. Use the heart, thumbs-down or comment icon to give feedback; use the star to save a product.

Feedback appends to `backend/data/feedback.csv`, including a UTC timestamp, session reference, job/product IDs, rating, reason, generated-image URL and product URL. Images are not downloaded; hosted URLs may expire.

## Photo checks and preview eligibility

On upload, the backend checks the clothing department and validates the image (maximum 10 MB and 25 megapixels), corrects orientation, removes metadata and resizes it. Muse Spark then assesses whether there is one clear main subject, a usable face, and which body areas are visible. Background people do not automatically disqualify a clear foreground subject. The same normalized photo's assessment is reused within its session.

The app uses the clothing department chosen by the user; it does **not** infer the person's gender from their photo.

A preview is skipped or unavailable when:

- No photo or clothing department has been provided, or photo assessment is pending, unsuccessful or unavailable.
- The main subject is ambiguous, the face is not usable, or the body area needed for the garment is not visible. An upper-body selfie can support a shirt while being unsuitable for trousers or shoes.
- Product department metadata conflicts with the selected department. If metadata is missing, the product must have been retrieved through that department's search filter; this is not independent department verification.
- The garment is underwear/lingerie, unsupported, or its category cannot be confirmed from metadata or fallback Muse Spark classification.
- No image-generation provider or product image is available.

Checks run before jobs are queued and again before generation. Replacing or removing the photo invalidates queued previews. Provider errors or timeouts show a failed preview with a retry option; a failed generation is different from an eligibility skip.

### What garment classification does

Garment classification identifies the product type so the backend can decide whether it can proceed to Muse Image for a personalized preview or must be skipped. It is one input to preview eligibility, alongside the selected clothing department and the uploaded photo assessment.

The backend groups products as `top`, `outerwear`, `bottom`, `dress`, `footwear`, `underwear` or `unknown`. It first reads Shopify category/product-type metadata. When the group remains unknown, a ready photo and configured image provider enable one batched, text-only Muse Spark request for the current page's unclassified products, using their titles and descriptions. Results are cached per session by product ID and text content.

The group determines which body areas must be visible: tops/jackets need the upper body, bottoms need the lower body, dresses need both, and shoes need visible feet. Underwear is excluded; unknown groups are skipped. This classifies the product type, not the person's gender, size or fit.

## Search and chat

Both accept natural-language fashion requests describing garment type, colour, material, size, style or budget. Examples:

- “Light blue shirts for a casual office look.”
- “Black oversized T-shirts under £40.”
- “Cotton jeans in size M.”

**Chat** uses Muse Spark to interpret the request, combine it with conversational context and create a `search_catalog` tool call with optional structured price, size and colour filters. Muse supplies the filter values; the backend forwards them to Shopify and preserves them during pagination. Follow-ups such as “Only cotton” or “Show blue instead” refine the previous search and update the product grid.

**The search bar** sends the entered query directly to Shopify's catalogue search through the backend MCP integration. It does not call Muse Spark to rewrite the query or retain chat history. Both routes use the selected clothing department and return up to 10 products per page.

These are product-discovery queries, not a general analytics chatbot. Chat turns explicit budgets, sizes and colours into Shopify filters; material and style stay in the query text. The search bar sends search text directly. Price bounds use minor currency units (for example, £30 is 3000); Shopify messages about price handling are displayed, and exact matching still depends on catalogue behavior and merchant metadata. Underwear and lingerie are excluded from this MVP. Multi-item outfit requests do not guarantee coordinated products or a combined outfit preview.

## Evaluation snapshot

Measured from `backend/data/evaluation.csv` on **4 November 2026, 13:38–13:47 UTC** (139 event records). These five rows summarize provider HTTP attempts in that file, including failed attempts. They are observed MVP results, not quality scores or guaranteed performance.

| Operation | Model / service | Successful / attempts | Mean latency (ms) | Mean latency (seconds) |
| --- | --- | --- | --- | --- |
| Shopify catalogue search | Shopify MCP | 4/4 | 514.818 | 0.515 |
| Conversational search | muse-spark-1.3 | 1/1 | 5,552.041 | 5.552 |
| Photo assessment | muse-spark-1.3 | 1/1 | 8,664.159 | 8.664 |
| Garment classification | muse-spark-1.3 | 2/2 | 8,705.766 | 8.706 |
| Image generation | muse-image-1.0 | 20/26 | 14,068.168 | 14.068 |

Image generation had **20 successful and 6 failed attempts**. These provider timings exclude preview queue waiting and do not measure the time to finish a full product page. The other model operations have small sample counts.

Download the same five rows: [evaluation-results.csv](docs/evaluation-results.csv). For token usage, queue timing and metric definitions, see [the evaluation guide](docs/evaluation.md).

To generate the full operational report from the project root:

```sh
cd backend
source .venv/bin/activate
python -m app.evaluation --output data/evaluation-summary.csv
```

This reads collected metrics; it makes no model calls.

## Verify

From the project root:

```sh
cd backend
.venv/bin/python -m pytest -q
cd ..
npm run build --prefix frontend
```

Filter format follows the official [Shopify search guide](https://shop.dev/docs/search) and [Catalog API reference](https://shop.dev/docs/catalog-api).

## Current MVP limitations

- One backend worker; in-memory sessions, chat history and jobs reset on restart.
- Each page displays up to 10 products and automatically submits them as a batch of up to 10 product IDs.
- Eligible products generate individually: 3 run concurrently while the remaining jobs wait in the queue.
- Each generated preview uses a separate model call; the batch is coordinated by the backend, not one model request for 10 images.
- 100 generation attempts per session per rolling hour; retries count, cached and blocked previews do not.
- The hourly quota resets on restart. It is not an account-wide or global spending cap.
- Submitted generation requests may continue after navigation and incur API charges.
- Unsuitable photos, unsupported garments or a missing provider can prevent previews.
- Generated-image URLs may expire. Photos and CSV files need persistent storage when deployed.
- Preview identity, colour, anatomy and fit can vary; exact sizing is not guaranteed.
- Feedback is user preference data, not a validated quality benchmark.
- Checkout happens on merchant websites; public hosting must be configured separately.

## Next steps

- **Voice search:** Let shoppers describe what they want aloud and refine results through conversation.
- **Better image selection:** Compare multiple merchant photos and select the clearest view of the garment, colour and details for each preview.
- **More natural lighting:** Adapt the preview to the shopper's photo lighting while preserving the product's colour and texture.
- **Saved photo profiles:** With the user's consent, securely save uploaded photos to their account so they can return without uploading again. Let them replace or delete photos at any time.
- **A more personal shopping experience:** Remember preferred sizes, styles and saved products, and let shoppers choose separate photo profiles when shopping for someone else.

## Deployment guide

See [Vercel + Render deployment steps](docs/deployment.md) for frontend/backend settings, secrets, storage and live verification.
