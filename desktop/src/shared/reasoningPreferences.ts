import { z } from 'zod';

export const reasoningPhaseSchema = z.enum(['all', 'discovery', 'planning', 'goals', 'verification']);
export const reasoningEffortSchema = z.enum(['auto', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']);
export const reasoningModelSchema = z.object({ provider: z.string().trim().min(1).max(80), model: z.string().trim().min(1).max(200) });
export const reasoningPreferencesSchema = z.partialRecord(reasoningPhaseSchema, reasoningEffortSchema);

export type ReasoningPhase = z.infer<typeof reasoningPhaseSchema>;
export type ReasoningEffort = z.infer<typeof reasoningEffortSchema>;
export type ReasoningModel = z.infer<typeof reasoningModelSchema>;
export type ReasoningPreferences = z.infer<typeof reasoningPreferencesSchema>;
export interface RepoReasoningPreferences { root: string; preferences: ReasoningPreferences }
