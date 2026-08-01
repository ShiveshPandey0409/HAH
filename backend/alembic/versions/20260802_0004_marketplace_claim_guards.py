"""Add marketplace and enrichment concurrency guarantees.

Revision ID: 20260802_0004
Revises: 20260802_0003
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0004"
down_revision: str | None = "20260802_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "social_accounts",
        sa.Column("enrichment_request_id", sa.Uuid(), nullable=True),
    )
    op.execute(
        """
        CREATE FUNCTION hah_claim_occupies_slot(
          p_status claim_status,
          p_claim_expires_at timestamptz
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
          SELECT p_status NOT IN ('expired', 'cancelled', 'rejected')
             AND NOT (
               p_status = 'claimed'
               AND p_claim_expires_at IS NOT NULL
               AND p_claim_expires_at <= now()
             )
        $$
        """
    )
    op.add_column("bounty_claims", sa.Column("reward_minor", sa.BigInteger(), nullable=True))
    op.add_column("bounty_claims", sa.Column("currency", sa.CHAR(length=3), nullable=True))
    op.execute(
        """
        UPDATE bounty_claims AS claim
           SET reward_minor = bounty.reward_minor,
               currency = task.currency
          FROM bounties AS bounty
          JOIN tasks AS task ON task.id = bounty.task_id
         WHERE bounty.id = claim.bounty_id
        """
    )
    op.alter_column("bounty_claims", "reward_minor", nullable=False)
    op.alter_column("bounty_claims", "currency", nullable=False)
    op.create_check_constraint(
        "bounty_claims_reward_minor_check",
        "bounty_claims",
        "reward_minor > 0",
    )
    op.create_check_constraint(
        "bounty_claims_currency_check",
        "bounty_claims",
        "currency = upper(currency)",
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_claim()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_bounty bounties%ROWTYPE;
          v_task tasks%ROWTYPE;
          v_social social_accounts%ROWTYPE;
          v_metric bigint;
          v_claimed integer;
          v_can_work boolean;
        BEGIN
          IF TG_OP = 'UPDATE'
             AND NEW.bounty_id IS NOT DISTINCT FROM OLD.bounty_id
             AND NEW.freelancer_id IS NOT DISTINCT FROM OLD.freelancer_id
             AND NEW.social_account_id IS NOT DISTINCT FROM OLD.social_account_id
             AND NOT (
               NOT hah_claim_occupies_slot(OLD.status, OLD.claim_expires_at)
               AND hah_claim_occupies_slot(NEW.status, NEW.claim_expires_at)
             ) THEN
            RETURN NEW;
          END IF;

          SELECT * INTO v_bounty
            FROM bounties
           WHERE id = NEW.bounty_id
           FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'HNF01', MESSAGE = 'bounty not found';
          END IF;
          IF v_bounty.status <> 'open' THEN
            RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'bounty is not open';
          END IF;
          IF v_bounty.deadline_at IS NOT NULL AND v_bounty.deadline_at <= now() THEN
            RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'bounty deadline has passed';
          END IF;

          SELECT * INTO v_task
            FROM tasks
           WHERE id = v_bounty.task_id
           FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'HNF01', MESSAGE = 'task not found';
          END IF;
          IF v_task.status <> 'open' THEN
            RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'task is not open';
          END IF;
          IF v_task.deadline_at IS NOT NULL AND v_task.deadline_at <= now() THEN
            RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'task deadline has passed';
          END IF;

          SELECT can_work_tasks INTO v_can_work
            FROM users
           WHERE id = NEW.freelancer_id
           FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'HNF01', MESSAGE = 'freelancer not found';
          END IF;
          IF NOT v_can_work THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'freelancer is not eligible to work';
          END IF;

          SELECT * INTO v_social
            FROM social_accounts
           WHERE id = NEW.social_account_id
           FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'HNF01', MESSAGE = 'social profile not found';
          END IF;
          IF v_social.user_id <> NEW.freelancer_id THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'social profile belongs to another freelancer';
          END IF;
          IF v_social.platform <> v_bounty.platform THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'social profile platform does not match bounty';
          END IF;
          IF NOT v_social.is_verified THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01', MESSAGE = 'social profile is not provider-validated';
          END IF;

          v_metric := CASE v_bounty.influence_metric
            WHEN 'followers' THEN v_social.follower_count
            WHEN 'karma' THEN v_social.karma
          END;
          IF v_metric IS NULL OR v_metric < v_bounty.min_influence
             OR (v_bounty.max_influence IS NOT NULL AND v_metric > v_bounty.max_influence) THEN
            RAISE EXCEPTION USING
              ERRCODE = 'HVL01',
              MESSAGE = 'public social profile does not meet the influence range';
          END IF;

          SELECT count(*) INTO v_claimed
            FROM bounty_claims
           WHERE bounty_id = NEW.bounty_id
             AND hah_claim_occupies_slot(status, claim_expires_at)
             AND id <> NEW.id;
          IF v_claimed >= v_bounty.slots_total THEN
            RAISE EXCEPTION USING ERRCODE = 'HCF01', MESSAGE = 'all bounty slots are claimed';
          END IF;

          NEW.platform := v_bounty.platform;
          IF NEW.claim_expires_at IS NOT NULL THEN
            IF v_bounty.deadline_at IS NOT NULL THEN
              NEW.claim_expires_at := LEAST(NEW.claim_expires_at, v_bounty.deadline_at);
            END IF;
            IF v_task.deadline_at IS NOT NULL THEN
              NEW.claim_expires_at := LEAST(NEW.claim_expires_at, v_task.deadline_at);
            END IF;
          END IF;
          IF TG_OP = 'INSERT' THEN
            NEW.reward_minor := v_bounty.reward_minor;
            NEW.currency := v_task.currency;
          ELSE
            NEW.reward_minor := OLD.reward_minor;
            NEW.currency := OLD.currency;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER claims_validate ON bounty_claims")
    op.execute(
        """
        CREATE TRIGGER claims_validate
        BEFORE INSERT OR UPDATE OF
          bounty_id, freelancer_id, social_account_id, status, claim_expires_at
        ON bounty_claims
        FOR EACH ROW EXECUTE FUNCTION validate_claim()
        """
    )

    op.execute(
        """
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
          INSERT INTO bounty_claims (
            bounty_id, freelancer_id, social_account_id, platform, claim_expires_at
          )
          SELECT b.id, p_freelancer_id, p_social_account_id, b.platform, now() + p_claim_ttl
            FROM bounties b WHERE b.id = p_bounty_id
          RETURNING * INTO v_result;
          IF v_result.id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'HNF01', MESSAGE = 'bounty not found';
          END IF;
          RETURN v_result;
        END;
        $$
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_bounty_claim_capacity()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          v_claimed integer;
        BEGIN
          IF NEW.slots_total >= OLD.slots_total THEN
            RETURN NEW;
          END IF;
          SELECT count(*) INTO v_claimed
            FROM bounty_claims
           WHERE bounty_id = NEW.id
             AND hah_claim_occupies_slot(status, claim_expires_at);
          IF v_claimed > NEW.slots_total THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              MESSAGE = 'bounty slots cannot be lower than active claims';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER bounties_enforce_claim_capacity
        BEFORE UPDATE OF slots_total ON bounties
        FOR EACH ROW EXECUTE FUNCTION enforce_bounty_claim_capacity()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_claim_reward_snapshot_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.reward_minor IS DISTINCT FROM OLD.reward_minor
             OR NEW.currency IS DISTINCT FROM OLD.currency THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514', MESSAGE = 'claim reward snapshot is immutable';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER claims_reward_snapshot_immutable
        BEFORE UPDATE OF reward_minor, currency ON bounty_claims
        FOR EACH ROW EXECUTE FUNCTION prevent_claim_reward_snapshot_change()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_claimed_bounty_pricing_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (NEW.task_id IS DISTINCT FROM OLD.task_id
              OR NEW.reward_minor IS DISTINCT FROM OLD.reward_minor)
             AND EXISTS (SELECT 1 FROM bounty_claims WHERE bounty_id = OLD.id) THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              MESSAGE = 'claimed bounty pricing and task cannot be changed';
          END IF;
          IF NEW.status = 'cancelled'
             AND NEW.status IS DISTINCT FROM OLD.status
             AND EXISTS (
               SELECT 1
                 FROM bounty_claims
                WHERE bounty_id = OLD.id
                  AND hah_claim_occupies_slot(status, claim_expires_at)
             ) THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              MESSAGE = 'bounty with active claims cannot be cancelled';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER bounties_claimed_pricing_immutable
        BEFORE UPDATE OF task_id, reward_minor, status ON bounties
        FOR EACH ROW EXECUTE FUNCTION prevent_claimed_bounty_pricing_change()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_claimed_task_currency_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.currency IS DISTINCT FROM OLD.currency AND EXISTS (
            SELECT 1
              FROM bounties AS bounty
              JOIN bounty_claims AS claim ON claim.bounty_id = bounty.id
             WHERE bounty.task_id = OLD.id
          ) THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              MESSAGE = 'task currency cannot change after a bounty is claimed';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tasks_claimed_currency_immutable
        BEFORE UPDATE OF currency ON tasks
        FOR EACH ROW EXECUTE FUNCTION prevent_claimed_task_currency_change()
        """
    )

    op.execute("DROP FUNCTION get_eligible_bounties(uuid)")
    op.execute(
        """
        CREATE FUNCTION get_eligible_bounties(p_freelancer_id uuid)
        RETURNS TABLE (
          bounty_id uuid,
          task_id uuid,
          task_title text,
          task_description text,
          bounty_title text,
          instructions text,
          platform social_platform,
          action bounty_action,
          reward_minor bigint,
          currency char(3),
          effective_deadline timestamptz,
          proof_requirements jsonb,
          remaining_slots bigint,
          social_account_id uuid
        )
        LANGUAGE sql STABLE AS $$
          SELECT b.id,
                 t.id,
                 t.title,
                 t.description,
                 b.title,
                 b.instructions,
                 b.platform,
                 b.action,
                 b.reward_minor,
                 t.currency,
                 CASE
                   WHEN b.deadline_at IS NULL THEN t.deadline_at
                   WHEN t.deadline_at IS NULL THEN b.deadline_at
                   ELSE LEAST(b.deadline_at, t.deadline_at)
                 END AS effective_deadline,
                 b.proof_requirements,
                 b.slots_total - count(c.id) AS remaining_slots,
                 sa.id
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
             AND hah_claim_occupies_slot(c.status, c.claim_expires_at)
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
          HAVING count(c.id) < b.slots_total
           ORDER BY effective_deadline NULLS LAST, b.id;
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION get_eligible_bounties(uuid)")
    op.execute(
        """
        CREATE FUNCTION get_eligible_bounties(p_freelancer_id uuid)
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
        $$
        """
    )

    op.execute("DROP TRIGGER claims_reward_snapshot_immutable ON bounty_claims")
    op.execute("DROP FUNCTION prevent_claim_reward_snapshot_change()")
    op.execute("DROP TRIGGER tasks_claimed_currency_immutable ON tasks")
    op.execute("DROP FUNCTION prevent_claimed_task_currency_change()")
    op.execute("DROP TRIGGER bounties_claimed_pricing_immutable ON bounties")
    op.execute("DROP FUNCTION prevent_claimed_bounty_pricing_change()")
    op.execute("DROP TRIGGER bounties_enforce_claim_capacity ON bounties")
    op.execute("DROP FUNCTION enforce_bounty_claim_capacity()")

    op.execute(
        """
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
        $$
        """
    )

    op.execute(
        """
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
        $$
        """
    )
    op.execute("DROP TRIGGER claims_validate ON bounty_claims")
    op.execute(
        """
        CREATE TRIGGER claims_validate
        BEFORE INSERT OR UPDATE OF bounty_id, freelancer_id, social_account_id
        ON bounty_claims
        FOR EACH ROW EXECUTE FUNCTION validate_claim()
        """
    )

    op.drop_constraint("bounty_claims_currency_check", "bounty_claims", type_="check")
    op.drop_constraint("bounty_claims_reward_minor_check", "bounty_claims", type_="check")
    op.drop_column("bounty_claims", "currency")
    op.drop_column("bounty_claims", "reward_minor")
    op.execute("DROP FUNCTION hah_claim_occupies_slot(claim_status, timestamptz)")
    op.drop_column("social_accounts", "enrichment_request_id")
