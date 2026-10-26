# Relayroom frontend

Relayroom is the user-facing control room for the Telegram Copier. It intentionally presents the workflow as a publishing dashboard: route health, message flow, source health, filter outcomes, and run history. Secrets and backend configuration never appear in the interface.

## Preview locally

From this directory:

```bash
python -m http.server 4173
```

Open http://localhost:4173.

The dashboard works in demo mode by default. To connect the **Run sync now** button to an Appwrite function execution URL, define this before loading `index.html`:

```html
<script>
  window.RELAYROOM_CONFIG = {
    functionUrl: 'https://<your-appwrite-endpoint>/v1/functions/telegram-copier-webhook/executions'
  };
</script>
```

The 3D relay core uses the open-source Three.js runtime from jsDelivr (MIT licensed) and is procedurally rendered in the browser, so no private backend data or paid 3D service is required.
