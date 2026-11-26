import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontendDir = resolve(here, "..");
const repoRoot = resolve(frontendDir, "..");
const scriptPath = resolve(repoRoot, "backend", "scripts", "export_openapi.py");
const outPath = resolve(frontendDir, "openapi.json");

const candidates = [
  resolve(repoRoot, ".venv", "Scripts", "python.exe"),
  resolve(repoRoot, ".venv", "bin", "python"),
  resolve(repoRoot, "backend", ".venv", "Scripts", "python.exe"),
  resolve(repoRoot, "backend", ".venv", "bin", "python"),
  "python3",
  "python",
];

let lastErr = "";
for (const bin of candidates) {
  if (bin.includes("/") || bin.includes("\\")) {
    if (!existsSync(bin)) continue;
  }
  const res = spawnSync(bin, [scriptPath, outPath], {
    cwd: repoRoot,
    encoding: "utf-8",
  });
  if (res.status === 0) {
    process.stdout.write(res.stdout || "");
    process.exit(0);
  }
  lastErr = res.stderr || res.error?.message || `exit ${res.status}`;
}

console.error("Failed to export OpenAPI schema from FastAPI backend:", lastErr);
process.exit(1);
