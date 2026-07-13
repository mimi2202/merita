const BASE = import.meta.env.VITE_API ?? 'http://127.0.0.1:8000';

export interface Receipt { rail: 'sap_escrow' | 'ace_x402'; leg: string; amount_atomic: number; token: string; tx: string | null; }
export interface Verdict { passed: boolean; confidence: number; reason: string; reveal_valid: boolean; }
export interface Bounty {
  id: string; title: string; spec: string; state: string;
  reward_lamports: number; claimed_by_pda: string | null;
  verdict: Verdict | null; receipts: Receipt[];
}
export interface Volume {
  countable: Record<string, number>; gross: Record<string, number>;
  quarantined_jobs: number; settled_jobs: number;
}

export const getBounties = (): Promise<Bounty[]> => fetch(`${BASE}/bounties`).then(r => r.json());
export const getVolume = (): Promise<Volume> => fetch(`${BASE}/volume`).then(r => r.json());
