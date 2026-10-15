// Talks to the Python mentor service over HTTP.
// Uses global fetch (available in the Node runtime that ships with modern VS Code).

export interface MentorMessage {
  kind: "blueprint" | "next_step" | "error" | "context_correction" | "context_question" | "understanding_check" | "stuck" | "explain" | "why" | "answer" | "fix";
  text: string;
  line?: number;
  curiosity?: string;
  blueprint?: string[];
  headline?: string;
  via?: string;
  replacement?: string;
  escalation_level?: number;
  intervention?: {
    learner_state: "progressing" | "thinking" | "stuck" | "off_path" | "error" | "finished";
    confidence: number;
    evidence: string[];
    should_intervene: boolean;
    hint_level: number;
    actions: Array<"another_hint" | "similar_example" | "wait" | "fix_and_explain">;
    intervention_id: string;
  };
  lesson_progress?: {
    current_step: number;
    completed_steps: number[];
    status: "in_progress" | "completed";
  };
  lesson_readiness?: LessonCheckResponse;
}

export interface EventOptions {
  idleSeconds?: number;
  target?: string; // for "explain"
  line?: number; // for "why" (1-based)
  symbol?: string; // for "why"
  question?: string; // for "ask"
  interventionId?: string;
  helpful?: boolean;
}

export interface GoalSuggestion {
  goal: string;
  rationale: string;
  module_id?: string;
}

export interface LevelProgress {
  key: string;
  title: string;
  mastered: number;
  total: number;
  done: boolean;
  skipped?: boolean;
  blurb: string;
}

export interface SuggestResponse {
  suggestions: GoalSuggestion[];
  path: { current_level: string; levels: LevelProgress[] };
  pathway_id?: string;
}

export interface SessionResponse {
  session_id: string;
  blueprint: string[];
  text: string;
  learner_level: "beginner" | "intermediate" | "advanced";
  pathway_id?: string;
  module_id?: string;
}

export interface ResumeResponse extends SessionResponse {
  goal: string;
  file_uri?: string;
  current_step: number;
  completed_steps: number[];
  status: "in_progress" | "completed";
  last_activity: number;
  checks: LessonCheck[];
  next_step: string;
  next?: { module_id: string; module_title: string; goal: string };
}

export interface LessonCheck { id: string; label: string; passed: boolean; }
export interface LessonCheckResponse {
  project_id: string;
  checks: LessonCheck[];
  passed: boolean;
  passed_count: number;
  total: number;
  next: { module_id: string; module_title: string; goal: string };
}

export interface HealthResponse {
  status: string;
  llm_mode: string;
  model: string;
  fast_model: string;
  providers: string[];
  local_model: boolean;
  tutoring_quality: "experimental" | "provider-dependent";
}

export class MentorClient {
  constructor(private baseUrl: string, private timeoutMs = 30_000) {}

  private lessonContext?: () => { sessionId: string; fileUri: string; version: number } | undefined;

  bindLessonContext(read: () => { sessionId: string; fileUri: string; version: number } | undefined): void {
    this.lessonContext = read;
  }

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

  async startSession(goal: string, learnerLevel: "beginner" | "intermediate" | "advanced",
                     pathwayId = "python-foundations", moduleId = ""): Promise<SessionResponse> {
    return this.post<SessionResponse>("/session", {
      goal, learner_level: learnerLevel, pathway_id: pathwayId, module_id: moduleId,
    });
  }

  async resume(learnerId = "local"): Promise<ResumeResponse | null> {
    const res = await this.fetchWithTimeout(
      `${this.baseUrl}/learner/${encodeURIComponent(learnerId)}/resume`
    );
    if (!res.ok) throw new Error(`resume ${res.status}`);
    const value = (await res.json()) as ResumeResponse | Record<string, never>;
    return (value as ResumeResponse).session_id ? value as ResumeResponse : null;
  }

  async checkLesson(sessionId: string, code: string, runPassed: boolean, fileUri = ""): Promise<LessonCheckResponse> {
    return this.post<LessonCheckResponse>("/lesson/check", {
      session_id: sessionId, code, run_passed: runPassed, file_uri: fileUri,
    });
  }

  async setLevel(sessionId: string, learnerLevel: "beginner" | "intermediate" | "advanced"): Promise<{ blueprint: string[] }> {
    return this.post<{ blueprint: string[] }>("/level", { session_id: sessionId, learner_level: learnerLevel });
  }

  async health(): Promise<HealthResponse> {
    const res = await this.fetchWithTimeout(`${this.baseUrl}/health`);
    if (!res.ok) throw new Error(`health ${res.status}`);
    return (await res.json()) as HealthResponse;
  }

  async suggestGoals(learnerId = "local", pathwayId = "python-foundations",
                     learnerLevel: "beginner" | "intermediate" | "advanced" = "beginner"): Promise<SuggestResponse> {
    const res = await this.fetchWithTimeout(
      `${this.baseUrl}/suggest-goal?learner_id=${encodeURIComponent(learnerId)}` +
      `&pathway_id=${encodeURIComponent(pathwayId)}&learner_level=${encodeURIComponent(learnerLevel)}`
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

  async reloadProvider(): Promise<HealthResponse> {
    return this.post<HealthResponse>("/provider/reload", {});
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
    type: "completed" | "error" | "stuck" | "hint" | "example" | "feedback" | "hover" | "explain" | "why" | "ask" | "fix",
    code: string,
    opts: EventOptions = {}
  ): Promise<MentorMessage | null> {
    const context = this.lessonContext?.();
    if (this.lessonContext && (!context || context.sessionId !== sessionId)) return null;
    const msg = await this.post<MentorMessage | Record<string, never>>("/event", {
      session_id: sessionId,
      type,
      code,
      idle_seconds: opts.idleSeconds,
      target: opts.target,
      line: opts.line,
      symbol: opts.symbol,
      question: opts.question,
      intervention_id: opts.interventionId,
      helpful: opts.helpful,
      file_uri: context?.fileUri,
      document_version: context?.version,
    });
    const current = this.lessonContext?.();
    if (context && (!current || current.sessionId !== context.sessionId ||
        current.fileUri !== context.fileUri || current.version !== context.version)) return null;
    if (!msg || !(msg as MentorMessage).kind) {
      return null;
    }
    return msg as MentorMessage;
  }
}
