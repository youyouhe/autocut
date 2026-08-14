import express from "express";
import path from "path";
import cors from "cors";
import { createServer as createViteServer } from "vite";
import multer from "multer";

async function startServer() {
  const app = express();
  const PORT = 3000;

  app.use(cors());
  app.use(express.json());

  // Set up multer for file uploads
  const upload = multer({ dest: 'render_uploads/' });

  // --- S01 Material Access & Cache Management API ---
  app.post("/api/upload", upload.any(), (req, res) => {
    const files = req.files as Express.Multer.File[];
    const result = files.map(file => {
      // Mock automatic classification
      let category = "other";
      if (file.mimetype.startsWith("video/")) category = "video";
      else if (file.mimetype.startsWith("image/")) category = "image";
      else if (file.mimetype.startsWith("audio/")) category = "audio";

      return {
        original_name: file.originalname,
        category,
        saved_name: file.filename,
        saved_path: file.path,
        file_size: file.size,
        saved_at: new Date().toISOString(),
        status: "ok"
      };
    });
    res.json(result);
  });

  app.get("/api/assets", (req, res) => {
    // Mock assets list
    res.json([
      { name: "demo_video.mp4", asset_type: "video", path: "render_uploads/demo_video.mp4", size: 10485760, modified_at: new Date().toISOString(), entry_type: "file" },
      { name: "cover.jpg", asset_type: "image", path: "render_uploads/cover.jpg", size: 512000, modified_at: new Date().toISOString(), entry_type: "file" }
    ]);
  });

  app.get("/api/video/serve", (req, res) => {
    // Mock video serving
    res.set("X-From-RAM", "true");
    res.send("video content bytes");
  });

  app.get("/api/memory/stats", (req, res) => {
    res.json({ videos_in_ram: 1, ram_used_mb: 10.0, ram_limit_mb: 500, analysis_count: 5, queried_at: new Date().toISOString() });
  });

  app.get("/api/memory/analysis", (req, res) => {
    res.json([]);
  });

  app.post("/api/localsend/start", (req, res) => {
    res.json({ status: "started" });
  });

  app.post("/api/localsend/stop", (req, res) => {
    res.json({ status: "stopped", received_files: [] });
  });

  app.get("/api/localsend/status", (req, res) => {
    res.json({
      running: true,
      device_name: "AI Video Workbench",
      port: 53317,
      active_session: null,
      pending_files: [],
      received_list: [],
      local_ip: "192.168.1.100",
      started_at: new Date().toISOString()
    });
  });

  // --- S02 Video Perception & Quality Check API ---
  app.post("/api/perceive", (req, res) => {
    res.json({
      path: req.body.path,
      meta: { duration: 15.5, width: 1080, height: 1920, fps: 30.0, source_path: req.body.path },
      scenes: { scene_count: 2, scenes: [{ scene_index: 0, time_sec: 0, time_text: "00:00.0" }, { scene_index: 1, time_sec: 5.5, time_text: "00:05.5" }] },
      visual_analysis: { content: "A person walking.", mood: "happy", quality: "8/10", highlights: [], suitable_for: ["vlog"], text_in_frame: "" },
      audio: { segments: [], full_text: "Hello world", asr_model: "mock" },
      cached: false,
      created_at: new Date().toISOString()
    });
  });

  app.get("/api/perceive/cached", (req, res) => {
    res.json({
      path: req.query.path,
      meta: { duration: 15.5, width: 1080, height: 1920, fps: 30.0, source_path: req.query.path },
      scenes: { scene_count: 2, scenes: [{ scene_index: 0, time_sec: 0, time_text: "00:00.0" }] },
      visual_analysis: { content: "A person walking.", mood: "happy", quality: "8/10", highlights: [], suitable_for: ["vlog"], text_in_frame: "" },
      audio: { segments: [], full_text: "Hello world", asr_model: "mock" },
      cached: true,
      created_at: new Date().toISOString()
    });
  });

  app.post("/perceive/video", upload.single('video'), (req, res) => {
    res.json({ message: "Video perceived" });
  });

  app.post("/perceive/result", upload.single('video'), (req, res) => {
    res.json({
      quality_score: 9.5,
      issues: ["None"],
      duration_ok: true,
      suggestions: ["Perfect"]
    });
  });

  // --- S04 Conversational Production API ---
  app.post("/api/chat", (req, res) => {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');

    const msg = req.body.message || "";
    
    // Mock SSE response
    res.write(`data: {"text": "I will help you with that. Let's analyze the video first.\\n"}\n\n`);
    
    setTimeout(() => {
      res.write(`data: {"tool": "get_resource_detail", "args": {"path": "demo_video.mp4"}, "result": {"success": true}}\n\n`);
    }, 500);

    setTimeout(() => {
      res.write(`data: {"text": "The video is about a person walking. I will create a draft and add the video.\\n"}\n\n`);
    }, 1000);

    setTimeout(() => {
      res.write(`data: {"tool": "create_draft", "args": {}, "result": {"draft_id": "draft-1234"}}\n\n`);
      res.write(`data: {"draft_id": "draft-1234"}\n\n`);
    }, 1500);

    setTimeout(() => {
      res.write(`data: {"tool": "add_video", "args": {"video_path": "demo_video.mp4"}, "result": {"success": true}}\n\n`);
    }, 2000);

    setTimeout(() => {
      res.write(`data: {"tool": "render", "args": {"draft_id": "draft-1234"}, "result": {"task_id": "task-5678"}}\n\n`);
      res.write(`data: [DONE]\n\n`);
      res.end();
    }, 2500);
  });

  // --- S07 Draft & Render Management API ---
  app.get("/api/drafts", (req, res) => {
    res.json([
      { folder: "draft-folder-1", draft_id: "draft-1234", draft_name: "My Awesome Video", duration: 15.5, cover_url: "/api/cover?folder=draft-folder-1", tm_draft_create: Date.now() - 100000, tm_draft_modified: Date.now() }
    ]);
  });

  app.get("/api/cover", (req, res) => {
    // Send a mock image or transparent pixel
    const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64");
    res.set("Content-Type", "image/png");
    res.send(pixel);
  });

  app.delete("/api/drafts/:folder", (req, res) => {
    res.json({ success: true });
  });

  app.post("/render", upload.single('draft'), (req, res) => {
    res.json({ task_id: "task-" + Math.floor(Math.random() * 10000) });
  });

  app.post("/render/draft/:draft_id", (req, res) => {
    res.json({ task_id: "task-" + Math.floor(Math.random() * 10000) });
  });

  app.get("/render/status/:task_id", (req, res) => {
    res.json({
      task_id: req.params.task_id,
      status: "done",
      draft_name: "My Awesome Video",
      mp4_name: "output.mp4",
      duration: 15.5,
      download_url: `/render/download/${req.params.task_id}`,
      message: ""
    });
  });

  app.get("/render/download/:task_id", (req, res) => {
    res.send("mock mp4 content");
  });

  app.get("/render/list", (req, res) => {
    res.json([
      { task_id: "task-5678", status: "done", draft_name: "My Awesome Video", mp4_name: "output.mp4", duration: 15.5, download_url: `/render/download/task-5678`, message: "" }
    ]);
  });

  // Health check
  app.get("/health", (req, res) => {
    res.json({ ok: true, service: "render_server", videos_dir: "C:\\Users\\Administrator\\Videos\\" });
  });

  // Vite middleware for development
  if (process.env.NODE_ENV !== "production") {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: "spa",
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(process.cwd(), 'dist');
    app.use(express.static(distPath));
    app.get('*all', (req, res) => {
      res.sendFile(path.join(distPath, 'index.html'));
    });
  }

  app.listen(PORT, "0.0.0.0", () => {
    console.log(`Server running on http://localhost:${PORT}`);
  });
}

startServer();
