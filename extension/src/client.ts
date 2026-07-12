// Talks to the Python mentor service over HTTP.
// Uses global fetch (available in the Node runtime that ships with modern VS Code).

export interface MentorMessage {
  kind: "blueprint" | "next_step" | "error" | "context_correction" | "context_question" | "understanding_check" | "stuck" | "explain" | "why" | "answer";
  text: string;
  line?: number;
  curiosity?: string;
  blueprint?: string[];
  headline?: string;
  via?: string;
}

export interface EventOptions {
  idleSeconds?: number;
  target?: string; // for "explain"
  line?: number; // for "why" (1-based)
  symbol?: string; // for "why"
  question?: string; // for "ask"
}

export interface GoalSuggestion {
  goal: string;
  rationale: string;
}

export interface LevelProgress {
  key: string;
  title: string;
  mastered: number;
  total: number;
  done: boolean;
  blurb: string;
}

export interface SuggestResponse {
  suggestions: GoalSuggestion[];
  path: { current_level: string; levels: LevelProgress[] };
}

export interface SessionResponse {
  session_id: string;
  blueprint: string[];
  text: string;
}

export class MentorClient {
  constructor(private baseUrl: string, private timeoutMs = 30_000) {}

  private async fetchWithTimeout(url: string, init?: RequestInit): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await fetch(url, { ...init, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const res = await this.fetchWithTimeout(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(`Mentor service ${res.status}: ${detail}`);
    }
    return (await res.json()) as T;
  }

  async startSession(goal: string): Promise<SessionResponse> {
    return this.post<SessionResponse>("/session", { goal });
  }

  async suggestGoals(learnerId = "local"): Promise<SuggestResponse> {
    const res = await this.fetchWithTimeout(
      `${this.baseUrl}/suggest-goal?learner_id=${encodeURIComponent(learnerId)}`
    );
    if (!res.ok) throw new Error(`suggest-goal ${res.status}`);
    return (await res.json()) as SuggestResponse;
  }

  async listModels(): Promise<{ models: string[]; default: string; fast: string }> {
    const res = await this.fetchWithTimeout(`${this.baseUrl}/models`);
    if (!res.ok) throw new Error(`models ${res.status}`);
    return (await res.json()) as { models: string[]; default: string; fast: string };
  }

  async setModel(sessionId: string, model: string): Promise<void> {
    await this.post("/model", { session_id: sessionId, model });
  }

  async getProfile(learnerId = "local"): Promise<{
    mastered: string[]; practiced: string[]; struggling: string[];
    recurring_misconceptions: string[];
  }> {
    const res = await this.fetchWithTimeout(
      `${this.baseUrl}/learner/${encodeURIComponent(learnerId)}/profile`
    );
    if (!res.ok) throw new Error(`profile ${res.status}`);
    return (await res.json()) as any;
  }

  async resetProfile(learnerId = "local"): Promise<void> {
    await this.post(`/learner/${encodeURIComponent(learnerId)}/reset`, {});
  }

  async sendEvent(
    sessionId: string,
    type: "completed" | "error" | "stuck" | "hover" | "explain" | "why" | "ask",
    code: string,
    opts: EventOptions = {}
  ): Promise<MentorMessage | null> {
    const msg = await this.post<MentorMessage | Record<string, never>>("/event", {
      session_id: sessionId,
      type,
      code,
      idle_seconds: opts.idleSeconds,
      target: opts.target,
      line: opts.line,
      symbol: opts.symbol,
      question: opts.question,
    });
    if (!msg || !(msg as MentorMessage).kind) {
      return null;
    }
    return msg as MentorMessage;
  }
}
