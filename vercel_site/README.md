# Slopodoro Vercel Dashboard

Static dashboard for the live Slopodoro WebSocket stream. Deploy this directory as a Vercel project.

```powershell
cd C:\Users\hocke\OneDrive\Documents\GitHub\Slopadoro\vercel_site
vercel --prod
```

The page reads a WebSocket stream from the `ws` query parameter:

```text
https://your-project.vercel.app/?ws=wss%3A%2F%2Fyour-live-stream.example
```

For local testing while `tools/hackathon_live_bridge.py` is running:

```text
http://localhost:8770/?ws=ws%3A%2F%2Flocalhost%3A8767%2F
```

Use the same `wss://` stream URL shown in the page's Extension URL field in the Chrome extension popup. The extension also accepts a hosted dashboard URL that contains `?ws=...` and extracts the stream from it. The bridge dashboard WebSocket is an extension-compatible superset: it includes `focus`, `fatigue`, `sources`, `subscores`, and the raw dashboard payload.

Vercel hosts this browser page only. The OpenBCI/Polar bridge must still run on the acquisition machine or on a WebSocket-capable relay.
