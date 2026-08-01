-- Hire-a-Human: requirements-only PostgreSQL schema
-- PostgreSQL 16+. Money is stored in minor units (for example, cents/paise).

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TYPE social_platform AS ENUM ('reddit', 'linkedin');
CREATE TYPE task_status AS ENUM ('draft', 'open', 'paused', 'completed', 'cancelled');
CREATE TYPE bounty_action AS ENUM ('post', 'comment');
CREATE TYPE bounty_status AS ENUM ('draft', 'open', 'closed', 'cancelled');
CREATE TYPE influence_metric AS ENUM ('followers', 'karma');
CREATE TYPE claim_status AS ENUM (
  'claimed', 'submitted', 'reviewing', 'changes_requested',
  'approved', 'rejected', 'paid', 'expired', 'cancelled'
);
CREATE TYPE verification_method AS ENUM ('automatic', 'manual', 'mcp');
CREATE TYPE verification_status AS ENUM ('pending', 'passed', 'failed', 'review_required');
CREATE TYPE authorization_status AS ENUM ('pending', 'active', 'paused', 'expired', 'cancelled');
CREATE TYPE payment_status AS ENUM ('created', 'processing', 'succeeded', 'failed', 'cancelled');
CREATE TYPE integration_status AS ENUM ('active', 'disabled');
CREATE TYPE request_status AS ENUM ('started', 'succeeded', 'failed');
CREATE TYPE delivery_status AS ENUM ('pending', 'retrying', 'delivered', 'failed');

-- One account can create tasks, work on tasks, or do both. Freelancer profile and
-- the user's single Prava account are merged here because both are 1:1 with a user.
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext NOT NULL UNIQUE,
  display_name text NOT NULL CHECK (btrim(display_name) <> ''),
  can_create_tasks boolean NOT NULL DEFAULT false,
  can_work_tasks boolean NOT NULL DEFAULT false,
  bio text,
  prava_account_ref text UNIQUE,
  prava_account_status text CHECK (prava_account_status IN ('pending', 'active', 'disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (can_create_tasks OR can_work_tasks),
  CHECK ((prava_account_ref IS NULL) = (prava_account_status IS NULL))
);

-- A freelancer can submit at most one public Reddit URL and one public LinkedIn URL.
-- Current enrichment metrics live here; a separate history table is unnecessary
-- for the current product requirements.
CREATE TABLE social_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  platform social_platform NOT NULL,
  profile_url text NOT NULL,
  follower_count bigint CHECK (follower_count >= 0),
  following_count bigint CHECK (following_count >= 0),
  reddit_post_karma bigint,
  reddit_comment_karma bigint,
  karma bigint GENERATED ALWAYS AS (
    CASE
      WHEN reddit_post_karma IS NULL AND reddit_comment_karma IS NULL THEN NULL
      ELSE COALESCE(reddit_post_karma, 0) + COALESCE(reddit_comment_karma, 0)
    END
  ) STORED,
  account_created_at timestamptz,
  is_verified boolean NOT NULL DEFAULT false,
  verified_at timestamptz,
  enrichment_provider text,
  enriched_at timestamptz,
  enrichment_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, platform),
  UNIQUE (platform, profile_url),
  UNIQUE (id, user_id, platform),
  CHECK (profile_url ~* '^https://[^[:space:]]+$'),
  CHECK (jsonb_typeof(enrichment_data) = 'object'),
  CHECK (platform = 'reddit' OR (reddit_post_karma IS NULL AND reddit_comment_karma IS NULL)),
  CHECK (NOT is_verified OR verified_at IS NOT NULL)
);

-- Top-level task/campaign owned directly by its creator. No organization layer.
CREATE TABLE tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  creator_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  title text NOT NULL CHECK (btrim(title) <> ''),
  description text NOT NULL,
  total_budget_minor bigint NOT NULL CHECK (total_budget_minor > 0),
  currency char(3) NOT NULL CHECK (currency = upper(currency)),
  status task_status NOT NULL DEFAULT 'draft',
  created_via text NOT NULL DEFAULT 'manual' CHECK (created_via IN ('manual', 'mcp')),
  deadline_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (deadline_at IS NULL OR deadline_at > created_at)
);

-- Each task can contain many paid pieces of work. Proof requirements are a small
-- JSON array (url/screenshot/image), so they are merged instead of normalized.
CREATE TABLE bounties (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  platform social_platform NOT NULL,
  action bounty_action NOT NULL,
  title text NOT NULL CHECK (btrim(title) <> ''),
  instructions text NOT NULL,
  reward_minor bigint NOT NULL CHECK (reward_minor > 0),
  slots_total integer NOT NULL DEFAULT 1 CHECK (slots_total > 0),
  influence_metric influence_metric NOT NULL,
  min_influence bigint NOT NULL DEFAULT 0 CHECK (min_influence >= 0),
  max_influence bigint CHECK (max_influence IS NULL OR max_influence >= 0),
  proof_requirements jsonb NOT NULL DEFAULT '["url"]'::jsonb,
  status bounty_status NOT NULL DEFAULT 'draft',
  deadline_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (max_influence IS NULL OR min_influence <= max_influence),
  CHECK (jsonb_typeof(proof_requirements) = 'array'),
  CHECK (jsonb_array_length(proof_requirements) > 0),
  CHECK (proof_requirements <@ '["url", "screenshot", "image"]'::jsonb),
  CHECK (
    (platform = 'reddit' AND influence_metric IN ('followers', 'karma')) OR
    (platform = 'linkedin' AND influence_metric = 'followers')
  )
);

-- One row reserves one bounty slot for one freelancer. It is the lifecycle spine
-- from claim through paid, so a separate pre-created slots table is not needed.
CREATE TABLE bounty_claims (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bounty_id uuid NOT NULL REFERENCES bounties(id) ON DELETE RESTRICT,
  freelancer_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  social_account_id uuid NOT NULL,
  platform social_platform NOT NULL,
  status claim_status NOT NULL DEFAULT 'claimed',
  claimed_at timestamptz NOT NULL DEFAULT now(),
  claim_expires_at timestamptz,
  submitted_at timestamptz,
  approved_at timestamptz,
  paid_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (social_account_id, freelancer_id, platform)
    REFERENCES social_accounts(id, user_id, platform) ON DELETE RESTRICT,
  UNIQUE (bounty_id, freelancer_id),
  CHECK (claim_expires_at IS NULL OR claim_expires_at > claimed_at)
);

-- Claims and submissions remain separate: a claim exists before proof is sent,
-- and one claim may be resubmitted after requested changes.
CREATE TABLE submissions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id uuid NOT NULL REFERENCES bounty_claims(id) ON DELETE RESTRICT,
  revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
  note text,
  verification_method verification_method,
  verification_status verification_status NOT NULL DEFAULT 'pending',
  verification_checks jsonb NOT NULL DEFAULT '{}'::jsonb,
  verifier_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  verification_note text,
  submitted_at timestamptz NOT NULL DEFAULT now(),
  verified_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (claim_id, revision),
  CHECK (jsonb_typeof(verification_checks) = 'object'),
  CHECK (
    verification_status IN ('pending', 'review_required') OR
    (verification_method IS NOT NULL AND verified_at IS NOT NULL)
  )
);

-- Proofs stay separate because a submission can contain a URL plus many images.
CREATE TABLE submission_proofs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  submission_id uuid NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('url', 'screenshot', 'image')),
  external_url text,
  storage_key text,
  mime_type text,
  sha256 text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (
    (kind = 'url' AND external_url IS NOT NULL AND storage_key IS NULL) OR
    (kind IN ('screenshot', 'image') AND storage_key IS NOT NULL)
  )
);

-- Prava's creator-approved allowance for automatic payments. This is separate
-- because one creator can authorize different caps for different tasks.
CREATE TABLE payment_authorizations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
  creator_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  provider text NOT NULL DEFAULT 'prava',
  provider_authorization_ref text UNIQUE,
  status authorization_status NOT NULL DEFAULT 'pending',
  per_payment_cap_minor bigint NOT NULL CHECK (per_payment_cap_minor > 0),
  total_cap_minor bigint NOT NULL CHECK (total_cap_minor > 0),
  used_minor bigint NOT NULL DEFAULT 0 CHECK (used_minor >= 0),
  max_payments integer CHECK (max_payments IS NULL OR max_payments > 0),
  payments_used integer NOT NULL DEFAULT 0 CHECK (payments_used >= 0),
  currency char(3) NOT NULL CHECK (currency = upper(currency)),
  valid_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (per_payment_cap_minor <= total_cap_minor),
  CHECK (used_minor <= total_cap_minor),
  CHECK (max_payments IS NULL OR payments_used <= max_payments)
);

-- One logical payout per approved claim. Provider retry details are kept in the
-- attempts table so the payment row stays stable and idempotent.
CREATE TABLE payments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  authorization_id uuid NOT NULL REFERENCES payment_authorizations(id) ON DELETE RESTRICT,
  task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
  bounty_id uuid NOT NULL REFERENCES bounties(id) ON DELETE RESTRICT,
  claim_id uuid NOT NULL UNIQUE REFERENCES bounty_claims(id) ON DELETE RESTRICT,
  submission_id uuid NOT NULL UNIQUE REFERENCES submissions(id) ON DELETE RESTRICT,
  payer_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  payee_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  provider text NOT NULL DEFAULT 'prava',
  amount_minor bigint NOT NULL CHECK (amount_minor > 0),
  currency char(3) NOT NULL CHECK (currency = upper(currency)),
  status payment_status NOT NULL DEFAULT 'created',
  idempotency_key text NOT NULL UNIQUE,
  provider_transaction_ref text UNIQUE,
  failure_code text,
  failure_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

CREATE TABLE payment_attempts (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  payment_id uuid NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  provider_session_ref text,
  provider_transaction_ref text,
  status payment_status NOT NULL,
  request_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  response_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (payment_id, attempt_number),
  CHECK (jsonb_typeof(request_data) = 'object'),
  CHECK (jsonb_typeof(response_data) = 'object'),
  CHECK (NOT request_data ?| ARRAY[
    'card_number', 'pan', 'cvv', 'cvc', 'security_code', 'expiry', 'expiry_date'
  ])
);

-- MCP/API client credentials belong directly to a creator user.
CREATE TABLE api_clients (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  creator_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name text NOT NULL,
  client_key text NOT NULL UNIQUE,
  secret_hash text NOT NULL,
  scopes text[] NOT NULL DEFAULT ARRAY[]::text[],
  status integration_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);

-- Audit/idempotency record for MCP task creation and verification calls.
CREATE TABLE mcp_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  api_client_id uuid NOT NULL REFERENCES api_clients(id) ON DELETE RESTRICT,
  method text NOT NULL,
  idempotency_key text NOT NULL,
  status request_status NOT NULL DEFAULT 'started',
  request_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  response_data jsonb,
  error_message text,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (api_client_id, idempotency_key),
  CHECK (jsonb_typeof(request_data) = 'object'),
  CHECK (response_data IS NULL OR jsonb_typeof(response_data) IN ('object', 'array'))
);

-- An endpoint is reusable configuration; each delivery is a retryable event.
CREATE TABLE webhook_endpoints (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  creator_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  url text NOT NULL,
  secret_hash text NOT NULL,
  subscribed_events text[] NOT NULL DEFAULT ARRAY[]::text[],
  status integration_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE webhook_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  endpoint_id uuid NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
  event_id uuid NOT NULL DEFAULT gen_random_uuid(),
  event_type text NOT NULL,
  entity_type text NOT NULL,
  entity_id uuid NOT NULL,
  payload jsonb NOT NULL,
  status delivery_status NOT NULL DEFAULT 'pending',
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at timestamptz,
  delivered_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (endpoint_id, event_id),
  CHECK (jsonb_typeof(payload) = 'object')
);

CREATE INDEX social_accounts_eligibility_idx
  ON social_accounts (platform, is_verified, follower_count, karma);
CREATE INDEX bounties_feed_idx ON bounties (platform, status, deadline_at);
CREATE INDEX claims_capacity_idx ON bounty_claims (bounty_id, status);
CREATE INDEX claims_freelancer_idx ON bounty_claims (freelancer_id, status);
CREATE INDEX webhook_retry_idx
  ON webhook_deliveries (status, next_attempt_at)
  WHERE status IN ('pending', 'retrying');
CREATE INDEX payment_retry_idx ON payments (status, updated_at)
  WHERE status IN ('created', 'processing', 'failed');
CREATE UNIQUE INDEX payment_attempts_provider_tx_unique
  ON payment_attempts (provider_transaction_ref)
  WHERE provider_transaction_ref IS NOT NULL;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER social_accounts_updated_at BEFORE UPDATE ON social_accounts
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER tasks_updated_at BEFORE UPDATE ON tasks
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER bounties_updated_at BEFORE UPDATE ON bounties
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER claims_updated_at BEFORE UPDATE ON bounty_claims
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER submissions_updated_at BEFORE UPDATE ON submissions
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER authorizations_updated_at BEFORE UPDATE ON payment_authorizations
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER payments_updated_at BEFORE UPDATE ON payments
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER webhook_endpoints_updated_at BEFORE UPDATE ON webhook_endpoints
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE FUNCTION validate_creator()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM users
    WHERE id = NEW.creator_id AND can_create_tasks
  ) THEN
    RAISE EXCEPTION 'creator must be an active user allowed to create tasks';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_validate_creator
BEFORE INSERT OR UPDATE OF creator_id ON tasks
FOR EACH ROW EXECUTE FUNCTION validate_creator();

-- Lock the task while editing bounties so concurrent edits cannot exceed budget.
CREATE OR REPLACE FUNCTION enforce_task_budget()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_task_id uuid;
  v_budget bigint;
  v_allocated numeric;
BEGIN
  v_task_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.task_id ELSE NEW.task_id END;
  SELECT total_budget_minor INTO v_budget FROM tasks WHERE id = v_task_id FOR UPDATE;
  SELECT COALESCE(sum(reward_minor::numeric * slots_total), 0)
    INTO v_allocated
    FROM bounties
   WHERE task_id = v_task_id
     AND status <> 'cancelled'
     AND id <> CASE WHEN TG_OP = 'INSERT' THEN NEW.id ELSE OLD.id END;

  IF TG_OP <> 'DELETE' AND NEW.status <> 'cancelled' THEN
    v_allocated := v_allocated + NEW.reward_minor::numeric * NEW.slots_total;
  END IF;

  IF v_allocated > v_budget THEN
    RAISE EXCEPTION 'bounties allocate % but task budget is %', v_allocated, v_budget;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER bounties_enforce_budget
BEFORE INSERT OR UPDATE OR DELETE ON bounties
FOR EACH ROW EXECUTE FUNCTION enforce_task_budget();

CREATE OR REPLACE FUNCTION enforce_task_budget_change()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_allocated numeric;
BEGIN
  SELECT COALESCE(sum(reward_minor::numeric * slots_total), 0)
    INTO v_allocated
    FROM bounties
   WHERE task_id = NEW.id AND status <> 'cancelled';
  IF v_allocated > NEW.total_budget_minor THEN
    RAISE EXCEPTION 'task budget cannot be lower than allocated bounties (%)', v_allocated;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_enforce_budget_change
BEFORE UPDATE OF total_budget_minor ON tasks
FOR EACH ROW EXECUTE FUNCTION enforce_task_budget_change();

CREATE OR REPLACE FUNCTION validate_claim()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_bounty bounties%ROWTYPE;
  v_social social_accounts%ROWTYPE;
  v_metric bigint;
  v_claimed integer;
BEGIN
  SELECT * INTO v_bounty FROM bounties WHERE id = NEW.bounty_id FOR UPDATE;
  IF NOT FOUND OR v_bounty.status <> 'open'
     OR (v_bounty.deadline_at IS NOT NULL AND v_bounty.deadline_at <= now()) THEN
    RAISE EXCEPTION 'bounty is not open';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM tasks
    WHERE id = v_bounty.task_id AND status = 'open'
      AND (deadline_at IS NULL OR deadline_at > now())
  ) THEN
    RAISE EXCEPTION 'task is not open';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM users
    WHERE id = NEW.freelancer_id AND can_work_tasks
  ) THEN
    RAISE EXCEPTION 'freelancer is not eligible to work';
  END IF;

  SELECT * INTO v_social
    FROM social_accounts
   WHERE id = NEW.social_account_id
     AND user_id = NEW.freelancer_id
     AND platform = v_bounty.platform
     AND is_verified;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'a provider-validated matching public social profile is required';
  END IF;

  v_metric := CASE v_bounty.influence_metric
    WHEN 'followers' THEN v_social.follower_count
    WHEN 'karma' THEN v_social.karma
  END;
  IF v_metric IS NULL OR v_metric < v_bounty.min_influence
     OR (v_bounty.max_influence IS NOT NULL AND v_metric > v_bounty.max_influence) THEN
    RAISE EXCEPTION 'public social profile does not meet the influence range';
  END IF;

  SELECT count(*) INTO v_claimed
    FROM bounty_claims
   WHERE bounty_id = NEW.bounty_id
     AND status NOT IN ('expired', 'cancelled', 'rejected')
     AND id <> NEW.id;
  IF v_claimed >= v_bounty.slots_total THEN
    RAISE EXCEPTION 'all bounty slots are claimed';
  END IF;

  NEW.platform := v_bounty.platform;
  RETURN NEW;
END;
$$;

CREATE TRIGGER claims_validate
BEFORE INSERT OR UPDATE OF bounty_id, freelancer_id, social_account_id ON bounty_claims
FOR EACH ROW EXECUTE FUNCTION validate_claim();

-- Freelancer feed: only open bounties matching a provider-validated public profile and its current
-- influencer range, excluding work already claimed by that freelancer.
CREATE OR REPLACE FUNCTION get_eligible_bounties(p_freelancer_id uuid)
RETURNS TABLE (
  bounty_id uuid,
  task_id uuid,
  title text,
  platform social_platform,
  action bounty_action,
  reward_minor bigint,
  currency char(3),
  open_slots bigint,
  social_account_id uuid
)
LANGUAGE sql STABLE AS $$
  SELECT b.id, t.id, b.title, b.platform, b.action, b.reward_minor, t.currency,
         b.slots_total - count(c.id) AS open_slots, sa.id
    FROM bounties b
    JOIN tasks t ON t.id = b.task_id
    JOIN social_accounts sa
      ON sa.user_id = p_freelancer_id
     AND sa.platform = b.platform
     AND sa.is_verified
     AND CASE b.influence_metric
       WHEN 'followers' THEN sa.follower_count
       WHEN 'karma' THEN sa.karma
     END >= b.min_influence
     AND (
       b.max_influence IS NULL OR
       CASE b.influence_metric
         WHEN 'followers' THEN sa.follower_count
         WHEN 'karma' THEN sa.karma
       END <= b.max_influence
     )
    LEFT JOIN bounty_claims c
      ON c.bounty_id = b.id
     AND c.status NOT IN ('expired', 'cancelled', 'rejected')
   WHERE b.status = 'open'
     AND t.status = 'open'
     AND (b.deadline_at IS NULL OR b.deadline_at > now())
     AND (t.deadline_at IS NULL OR t.deadline_at > now())
     AND EXISTS (
       SELECT 1 FROM users u
       WHERE u.id = p_freelancer_id AND u.can_work_tasks
     )
     AND NOT EXISTS (
       SELECT 1 FROM bounty_claims mine
       WHERE mine.bounty_id = b.id AND mine.freelancer_id = p_freelancer_id
     )
   GROUP BY b.id, t.id, t.currency, sa.id
  HAVING count(c.id) < b.slots_total;
$$;

-- Atomic claim API. The bounty row lock serializes competing claims.
CREATE OR REPLACE FUNCTION claim_bounty(
  p_bounty_id uuid,
  p_freelancer_id uuid,
  p_social_account_id uuid,
  p_claim_ttl interval DEFAULT interval '24 hours'
)
RETURNS bounty_claims
LANGUAGE plpgsql AS $$
DECLARE
  v_result bounty_claims;
BEGIN
  PERFORM 1 FROM bounties WHERE id = p_bounty_id FOR UPDATE;
  INSERT INTO bounty_claims (
    bounty_id, freelancer_id, social_account_id, platform, claim_expires_at
  )
  SELECT b.id, p_freelancer_id, p_social_account_id, b.platform, now() + p_claim_ttl
    FROM bounties b WHERE b.id = p_bounty_id
  RETURNING * INTO v_result;
  RETURN v_result;
END;
$$;

CREATE OR REPLACE FUNCTION apply_submission_state()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE bounty_claims
     SET status = 'submitted', submitted_at = NEW.submitted_at
   WHERE id = NEW.claim_id
     AND status IN ('claimed', 'changes_requested');
  IF NOT FOUND THEN
    RAISE EXCEPTION 'claim cannot accept a submission in its current state';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER submissions_apply_state
AFTER INSERT ON submissions
FOR EACH ROW EXECUTE FUNCTION apply_submission_state();

CREATE OR REPLACE FUNCTION apply_verification_state()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.verification_status = 'passed' THEN
    IF EXISTS (
      SELECT 1
        FROM bounty_claims c
        JOIN bounties b ON b.id = c.bounty_id
        CROSS JOIN LATERAL jsonb_array_elements_text(b.proof_requirements) required(kind)
       WHERE c.id = NEW.claim_id
         AND NOT EXISTS (
           SELECT 1 FROM submission_proofs p
            WHERE p.submission_id = NEW.id AND p.kind = required.kind
         )
    ) THEN
      RAISE EXCEPTION 'submission is missing a required proof type';
    END IF;
    UPDATE bounty_claims
       SET status = 'approved', approved_at = NEW.verified_at
     WHERE id = NEW.claim_id AND status IN ('submitted', 'reviewing');
  ELSIF NEW.verification_status = 'failed' THEN
    UPDATE bounty_claims SET status = 'rejected'
     WHERE id = NEW.claim_id AND status IN ('submitted', 'reviewing');
  ELSIF NEW.verification_status = 'review_required' THEN
    UPDATE bounty_claims SET status = 'reviewing'
     WHERE id = NEW.claim_id AND status = 'submitted';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER submissions_apply_verification
AFTER UPDATE OF verification_status ON submissions
FOR EACH ROW WHEN (OLD.verification_status IS DISTINCT FROM NEW.verification_status)
EXECUTE FUNCTION apply_verification_state();

CREATE OR REPLACE FUNCTION validate_payment_authorization()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_task tasks%ROWTYPE;
BEGIN
  SELECT * INTO v_task FROM tasks WHERE id = NEW.task_id;
  IF NOT FOUND OR NEW.creator_id <> v_task.creator_id OR NEW.currency <> v_task.currency THEN
    RAISE EXCEPTION 'payment authorization must match the task creator and currency';
  END IF;
  IF NEW.total_cap_minor > v_task.total_budget_minor THEN
    RAISE EXCEPTION 'payment authorization cannot exceed task budget';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER payment_authorizations_validate
BEFORE INSERT OR UPDATE OF task_id, creator_id, currency, total_cap_minor
ON payment_authorizations
FOR EACH ROW EXECUTE FUNCTION validate_payment_authorization();

CREATE OR REPLACE FUNCTION validate_payment()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_task tasks%ROWTYPE;
  v_bounty bounties%ROWTYPE;
  v_claim bounty_claims%ROWTYPE;
  v_submission submissions%ROWTYPE;
  v_auth payment_authorizations%ROWTYPE;
BEGIN
  SELECT * INTO v_task FROM tasks WHERE id = NEW.task_id;
  SELECT * INTO v_bounty FROM bounties WHERE id = NEW.bounty_id;
  SELECT * INTO v_claim FROM bounty_claims WHERE id = NEW.claim_id FOR UPDATE;
  SELECT * INTO v_submission FROM submissions WHERE id = NEW.submission_id;
  SELECT * INTO v_auth FROM payment_authorizations WHERE id = NEW.authorization_id FOR UPDATE;

  IF v_bounty.task_id <> v_task.id OR v_claim.bounty_id <> v_bounty.id
     OR v_submission.claim_id <> v_claim.id OR v_submission.verification_status <> 'passed'
     OR v_claim.status <> 'approved' THEN
    RAISE EXCEPTION 'payment must match an approved submission and its task/bounty';
  END IF;
  IF NEW.payer_user_id <> v_task.creator_id OR NEW.payee_user_id <> v_claim.freelancer_id THEN
    RAISE EXCEPTION 'payment payer/payee do not match creator/freelancer';
  END IF;
  IF NEW.amount_minor <> v_bounty.reward_minor OR NEW.currency <> v_task.currency THEN
    RAISE EXCEPTION 'payment amount/currency do not match the bounty';
  END IF;
  IF v_auth.task_id <> v_task.id OR v_auth.creator_id <> v_task.creator_id
     OR v_auth.status <> 'active' OR v_auth.currency <> v_task.currency
     OR NEW.amount_minor > v_auth.per_payment_cap_minor
     OR v_auth.used_minor + NEW.amount_minor > v_auth.total_cap_minor
     OR (v_auth.max_payments IS NOT NULL AND v_auth.payments_used >= v_auth.max_payments)
     OR (v_auth.valid_until IS NOT NULL AND v_auth.valid_until <= now()) THEN
    RAISE EXCEPTION 'payment authorization is missing, inactive, expired, or over limit';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER payments_validate
BEFORE INSERT ON payments
FOR EACH ROW EXECUTE FUNCTION validate_payment();

CREATE OR REPLACE FUNCTION apply_payment_success()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_auth payment_authorizations%ROWTYPE;
BEGIN
  IF NEW.status = 'succeeded' AND OLD.status IS DISTINCT FROM NEW.status THEN
    SELECT * INTO v_auth FROM payment_authorizations
     WHERE id = NEW.authorization_id FOR UPDATE;
    IF v_auth.status <> 'active'
       OR v_auth.used_minor + NEW.amount_minor > v_auth.total_cap_minor
       OR (v_auth.max_payments IS NOT NULL AND v_auth.payments_used >= v_auth.max_payments)
       OR (v_auth.valid_until IS NOT NULL AND v_auth.valid_until <= now()) THEN
      RAISE EXCEPTION 'payment success would exceed or violate its authorization';
    END IF;
    UPDATE bounty_claims SET status = 'paid', paid_at = COALESCE(NEW.completed_at, now())
     WHERE id = NEW.claim_id AND status = 'approved';
    UPDATE payment_authorizations
       SET used_minor = used_minor + NEW.amount_minor,
           payments_used = payments_used + 1
     WHERE id = NEW.authorization_id;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER payments_apply_success
AFTER UPDATE OF status ON payments
FOR EACH ROW EXECUTE FUNCTION apply_payment_success();

COMMENT ON TABLE users IS 'Creator and/or freelancer account, including 1:1 profile and Prava account reference.';
COMMENT ON TABLE social_accounts IS 'Freelancer-submitted public Reddit/LinkedIn account URL, provider validation, and latest influence metrics; no username or OAuth connection.';
COMMENT ON TABLE tasks IS 'Creator-owned campaign with a total budget.';
COMMENT ON TABLE bounties IS 'Paid Reddit/LinkedIn post or comment subtask with eligibility and proof rules.';
COMMENT ON TABLE bounty_claims IS 'One freelancer reservation/slot and its work lifecycle.';
COMMENT ON TABLE submissions IS 'A claim submission revision plus its current verification result.';
COMMENT ON TABLE submission_proofs IS 'One or more URLs/screenshots/images attached to a submission.';
COMMENT ON TABLE payment_authorizations IS 'Prava authorization and limits for automatic task payouts.';
COMMENT ON TABLE payments IS 'One idempotent logical payout for an approved claim.';
COMMENT ON TABLE payment_attempts IS 'Retry/audit trail for calls to the payment provider; never store PAN/CVV.';
COMMENT ON TABLE api_clients IS 'MCP/API credentials owned by a creator.';
COMMENT ON TABLE mcp_requests IS 'Idempotent audit record for agent calls.';
COMMENT ON TABLE webhook_endpoints IS 'Creator webhook configuration.';
COMMENT ON TABLE webhook_deliveries IS 'Retryable submission/verification/payment event sent to a webhook.';
