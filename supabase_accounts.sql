-- À exécuter une seule fois dans Supabase : SQL Editor > New query.
-- Les mots de passe restent hachés dans la colonne JSONB : ils ne sont jamais
-- envoyés au navigateur.

create table if not exists public.nathgpt_accounts (
    username text primary key check (char_length(username) between 1 and 80),
    data jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create or replace function public.nathgpt_set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists nathgpt_accounts_updated_at on public.nathgpt_accounts;
create trigger nathgpt_accounts_updated_at
before update on public.nathgpt_accounts
for each row execute function public.nathgpt_set_updated_at();

-- Aucune donnée n'est accessible depuis le navigateur. Seul le serveur
-- Render utilisant la clé secrète Supabase peut lire et écrire la table.
alter table public.nathgpt_accounts enable row level security;
revoke all on table public.nathgpt_accounts from anon, authenticated;
grant select, insert, update, delete on table public.nathgpt_accounts to service_role;
