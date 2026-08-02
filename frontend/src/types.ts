export type Platform = 'reddit' | 'linkedin'
export type BountyAction = 'post' | 'comment'
export type ProofType = 'url' | 'screenshot' | 'image'
export type TaskStatus = 'draft' | 'open' | 'paused' | 'completed' | 'cancelled'

export interface User {
  id: string
  email: string
  display_name: string
  can_create_tasks: boolean
  can_work_tasks: boolean
  bio: string | null
  created_at: string
  updated_at: string
}

export interface AuthResponse {
  access_token: string
  token_type: 'bearer'
  expires_at: string
  user: User
}

export interface BountyInput {
  platform: Platform
  action: BountyAction
  title: string
  instructions: string
  reward_minor: number
  slot_count: number
  influence_metric: 'followers' | 'karma'
  min_influence: number
  max_influence: number | null
  proof_requirements: ProofType[]
  deadline_at: string | null
}

export interface TaskInput {
  title: string
  description: string
  total_budget_minor: number
  currency: string
  deadline_at: string | null
  bounties: BountyInput[]
}

export interface Bounty extends BountyInput {
  id: string
  status: 'draft' | 'open' | 'closed' | 'cancelled'
  claim_count: number
  remaining_slots: number
  created_at: string
  updated_at: string
}

export interface Task extends Omit<TaskInput, 'bounties'> {
  id: string
  creator_id: string
  allocated_budget_minor: number
  remaining_budget_minor: number
  status: TaskStatus
  created_via: 'manual' | 'mcp'
  bounties: Bounty[]
  created_at: string
  updated_at: string
}

export interface SocialProfile {
  id: string
  user_id: string
  platform: Platform
  profile_url: string
  follower_count: number | null
  following_count: number | null
  reddit_post_karma: number | null
  reddit_comment_karma: number | null
  karma: number | null
  account_created_at: string | null
  is_verified: boolean
  verified_at: string | null
  enrichment_provider: string | null
  enriched_at: string | null
  created_at: string
  updated_at: string
}

export interface EligibleBounty {
  bounty_id: string
  task_id: string
  task_title: string
  task_description: string
  bounty_title: string
  instructions: string
  platform: Platform
  action: BountyAction
  reward_minor: number
  currency: string
  effective_deadline: string | null
  proof_requirements: ProofType[]
  remaining_slots: number
  social_account_id: string
}

export interface Claim {
  id: string
  bounty_id: string
  freelancer_id: string
  social_account_id: string
  platform: Platform
  status: string
  reward_minor: number
  currency: string
  claimed_at: string
  claim_expires_at: string | null
  updated_at: string
}

export interface SubmissionProofInput {
  proof_type: ProofType
  url?: string | null
  upload_id?: string | null
}

export interface SubmissionProof {
  id: string
  proof_type: ProofType
  url: string | null
  storage_key?: string | null
  upload_id: string | null
  mime_type?: string | null
  sha256?: string | null
  size_bytes: number | null
  content_url: string | null
}

export interface Submission {
  id: string
  claim_id: string
  revision: number
  proofs: SubmissionProof[]
  verification_method: 'automatic' | 'manual' | 'mcp' | null
  verification_status: 'pending' | 'passed' | 'failed' | 'review_required'
  checks: Record<string, unknown>
  verifier_user_id: string | null
  failure_reason: string | null
  claim_status: string
  submitted_at: string
  verified_at: string | null
  updated_at: string
}

export interface ProofUpload {
  upload_id: string
  claim_id: string
  proof_type: 'screenshot' | 'image'
  mime_type: string
  sha256: string
  size_bytes: number
  created_at: string
}

export interface WorkClaim extends Claim {
  task_id: string
  task_title: string
  bounty_title: string
  instructions: string
  proof_requirements: ProofType[]
  submission: Submission | null
}

export type PaymentStatus = 'created' | 'processing' | 'succeeded' | 'failed' | 'cancelled'

export interface PaymentAuthorization {
  id: string
  task_id: string
  provider: string
  status: 'pending' | 'active' | 'paused' | 'expired' | 'cancelled'
  total_cap_minor: number
  used_minor: number
  remaining_minor: number
  currency: string
  funding_status: PaymentStatus
  approval_url: string | null
  approval_expires_at: string | null
  valid_until: string | null
  funding_failure_message: string | null
  reused_global_approval: boolean
}

export interface Payment {
  id: string
  task_id: string
  bounty_id: string
  claim_id: string
  submission_id: string
  payer_user_id: string
  payee_user_id: string
  provider: string
  amount_minor: number
  currency: string
  status: PaymentStatus
  provider_transaction_ref: string | null
  failure_code: string | null
  failure_message: string | null
  next_attempt_at: string | null
  attempt_count: number
  created_at: string
  updated_at: string
  completed_at: string | null
}

export interface Wallet {
  user_id: string
  redeemable: boolean
  balances: { currency: string; balance_minor: number }[]
  entries: { id: string; payment_id: string; amount_minor: number; currency: string; entry_type: string; created_at: string }[]
}

export interface WebhookEndpoint {
  id: string
  creator_id: string
  url: string
  subscribed_events: WebhookEvent[]
  status: string
  delivery: { success_statuses: '200-299'; max_attempts: number; timeout_seconds: number }
  signing_secret?: string
  created_at: string
  updated_at: string
}

export type WebhookEvent =
  | 'submission.created'
  | 'verification.completed'
  | 'mcp_request.completed'
