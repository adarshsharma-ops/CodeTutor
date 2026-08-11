import * as fs from "fs/promises";
import * as path from "path";

export type ProviderEnv = Record<string, string>;

/** Update selected keys without exposing or deleting unrelated values in .env. */
export function mergeEnv(text: string, updates: ProviderEnv): string {
  const values = new Map(Object.entries(updates));
  const written = new Set<string>();
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const output = lines.map((line) => {
    const match = line.match(/^([A-Z][A-Z0-9_]*)=/);
    if (!match || !values.has(match[1])) return line;
    // Old hand-edited files may contain duplicate provider keys. Keep one canonical
    // value so configuration order cannot silently select the wrong model.
    if (written.has(match[1])) return "";
    const value = values.get(match[1])!;
    written.add(match[1]);
    return `${match[1]}=${value}`;
  });
  if (output.length && output[output.length - 1] !== "") output.push("");
  for (const [key, value] of values) if (!written.has(key)) output.push(`${key}=${value}`);
  return `${output.join("\n").replace(/\n{3,}/g, "\n\n").replace(/\n+$/, "")}\n`;
}

export async function updateEnvFile(envPath: string, updates: ProviderEnv): Promise<void> {
  let current = "";
  try { current = await fs.readFile(envPath, "utf8"); }
  catch (error: any) { if (error?.code !== "ENOENT") throw error; }
  await fs.mkdir(path.dirname(envPath), { recursive: true });
  const temporary = `${envPath}.codetutor-${process.pid}`;
  await fs.writeFile(temporary, mergeEnv(current, updates), { encoding: "utf8", mode: 0o600 });
  await fs.rename(temporary, envPath);
  await fs.chmod(envPath, 0o600);
}

export function validApiKey(value: string): boolean {
  return value.trim().length >= 16 && !/\s/.test(value);
}
