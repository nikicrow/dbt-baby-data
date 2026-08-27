--
-- ci/source_schema.sql — DDL snapshot of the app's source tables.
--
-- WHY THIS FILE EXISTS
-- CI spins up an empty postgres service container, and
-- baby_data/scripts/load_to_database.py only ever INSERTs: it calls
-- check_table_exists() and aborts if a table is missing, and never issues
-- DDL. So something has to create the schema first. That something is this
-- file, replayed with `psql -f` before run_pipeline.py runs.
--
-- WHAT IT CONTAINS
-- The whole `public` schema of the app database: the six source tables read by
-- baby_data/models/raw/sources.yml (baby_profiles, diaper_events,
-- feeding_sessions, sleep_sessions, growth_measurements, health_events), the
-- 13 enum types their columns depend on, and alembic_version. The enums are
-- the reason this is a whole-schema dump rather than a `-t`-filtered one:
-- filtering by table omits the CREATE TYPE statements and the replay fails.
--
-- *** REGENERATE THIS FILE WHENEVER AN ALEMBIC MIGRATION LANDS. ***
-- It is a point-in-time copy, not a live view, so it silently drifts from the
-- app's models otherwise. It is pinned to Alembic head e7a91b4c2d58 (see the
-- alembic_version seed at the end). A migration that adds a column will make
-- CI fail loudly at the load or dbt step rather than testing the real schema.
--
-- Regenerate with:
--   pg_dump --schema-only --no-owner --no-privileges --no-comments \
--     -h <host> -U postgres -d baby_data -n public > ci/source_schema.sql
-- then re-apply the two local edits below: the CREATE SCHEMA guard, and the
-- alembic_version seed at the foot of the file.
--
-- PROVENANCE
-- Generated 2026-08-27 from the *laptop's* Postgres 15.7 copy of baby_data,
-- not from the authoritative database on fedora-1. The laptop's data is a
-- stale rollback copy frozen ~2026-08-25, but its *schema* is current: both
-- are at Alembic head e7a91b4c2d58 and no migration has landed since. Postgres
-- on fedora-1 listens on localhost only and is not reachable over the tailnet,
-- so it could not be dumped directly. Prefer dumping from fedora-1 when
-- regenerating.
--
-- Note the version skew: this was dumped by pg_dump 15.7, while fedora-1 runs
-- 17.10 and CI replays it into postgres:17. That direction is safe — a 15 dump
-- is plain SQL that 17 accepts — but a snapshot regenerated on fedora-1 with
-- pg_dump 17 is the more faithful artifact, so take it there when you can.
--

--
-- PostgreSQL database dump
--

-- Dumped from database version 15.7
-- Dumped by pg_dump version 15.7

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- Local edit: the postgres image already ships a public schema, so a bare
-- CREATE SCHEMA aborts the replay under ON_ERROR_STOP=1.
CREATE SCHEMA IF NOT EXISTS public;


--
-- Name: appetite; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.appetite AS ENUM (
    'POOR',
    'FAIR',
    'GOOD',
    'EXCELLENT'
);


--
-- Name: breastside; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.breastside AS ENUM (
    'LEFT',
    'RIGHT'
);


--
-- Name: diapertype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.diapertype AS ENUM (
    'DISPOSABLE',
    'CLOTH',
    'TRAINING'
);


--
-- Name: feedingtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.feedingtype AS ENUM (
    'BREAST',
    'BOTTLE',
    'SOLID'
);


--
-- Name: healtheventtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.healtheventtype AS ENUM (
    'VACCINATION',
    'ILLNESS',
    'MEDICATION',
    'MILESTONE',
    'DOCTOR_VISIT',
    'ALLERGY',
    'OTHER'
);


--
-- Name: measurementcontext; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.measurementcontext AS ENUM (
    'HOME',
    'DOCTOR_VISIT',
    'HOSPITAL',
    'CLINIC'
);


--
-- Name: sleeplocation; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.sleeplocation AS ENUM (
    'CRIB',
    'BASSINET',
    'PARENT_BED',
    'STROLLER',
    'CAR_SEAT',
    'OTHER'
);


--
-- Name: sleepquality; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.sleepquality AS ENUM (
    'RESTLESS',
    'FAIR',
    'GOOD',
    'DEEP'
);


--
-- Name: sleeptype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.sleeptype AS ENUM (
    'NAP',
    'NIGHTTIME'
);


--
-- Name: stoolcolor; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.stoolcolor AS ENUM (
    'YELLOW',
    'BROWN',
    'GREEN',
    'RED',
    'BLACK',
    'OTHER'
);


--
-- Name: stoolconsistency; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.stoolconsistency AS ENUM (
    'LIQUID',
    'SOFT',
    'FORMED',
    'HARD'
);


--
-- Name: urinevolume; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.urinevolume AS ENUM (
    'NONE',
    'LIGHT',
    'MODERATE',
    'HEAVY'
);


--
-- Name: wakereason; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.wakereason AS ENUM (
    'NATURAL',
    'CRYING',
    'FEEDING',
    'DIAPER',
    'NOISE',
    'OTHER'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: baby_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.baby_profiles (
    id uuid NOT NULL,
    name character varying(100) NOT NULL,
    date_of_birth date NOT NULL,
    birth_weight double precision,
    birth_length double precision,
    birth_head_circumference double precision,
    gender character varying(20),
    timezone character varying(50),
    notes text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    is_active boolean,
    source character varying(20) DEFAULT 'app'::character varying NOT NULL
);


--
-- Name: diaper_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.diaper_events (
    id uuid NOT NULL,
    baby_id uuid NOT NULL,
    "timestamp" timestamp without time zone NOT NULL,
    has_urine boolean,
    urine_volume public.urinevolume,
    has_stool boolean,
    stool_consistency public.stoolconsistency,
    stool_color public.stoolcolor,
    diaper_type public.diapertype,
    notes text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL,
    source character varying(20) DEFAULT 'app'::character varying NOT NULL
);


--
-- Name: feeding_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.feeding_sessions (
    id uuid NOT NULL,
    baby_id uuid NOT NULL,
    start_time timestamp without time zone NOT NULL,
    end_time timestamp without time zone,
    feeding_type public.feedingtype NOT NULL,
    breast_started public.breastside,
    left_breast_duration integer,
    right_breast_duration integer,
    volume_offered_ml integer,
    volume_consumed_ml integer,
    formula_type character varying(100),
    food_items json,
    appetite public.appetite,
    notes text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL,
    source character varying(20) DEFAULT 'app'::character varying NOT NULL
);


--
-- Name: growth_measurements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.growth_measurements (
    id uuid NOT NULL,
    baby_id uuid NOT NULL,
    measurement_date date NOT NULL,
    weight_kg double precision,
    length_cm double precision,
    head_circumference_cm double precision,
    measurement_context public.measurementcontext,
    measured_by character varying(100),
    percentiles json,
    notes text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL,
    source character varying(20) DEFAULT 'app'::character varying NOT NULL
);


--
-- Name: health_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.health_events (
    id uuid NOT NULL,
    baby_id uuid NOT NULL,
    event_date timestamp without time zone NOT NULL,
    event_type public.healtheventtype NOT NULL,
    title character varying(200) NOT NULL,
    description text,
    temperature_celsius double precision,
    symptoms json,
    treatment text,
    healthcare_provider character varying(200),
    follow_up_required boolean,
    follow_up_date timestamp without time zone,
    attachments json,
    notes text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL,
    source character varying(20) DEFAULT 'app'::character varying NOT NULL
);


--
-- Name: sleep_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sleep_sessions (
    id uuid NOT NULL,
    baby_id uuid NOT NULL,
    start_time timestamp without time zone NOT NULL,
    end_time timestamp without time zone,
    sleep_type public.sleeptype NOT NULL,
    location public.sleeplocation,
    sleep_quality public.sleepquality,
    sleep_environment json,
    wake_reason public.wakereason,
    notes text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL,
    source character varying(20) DEFAULT 'app'::character varying NOT NULL
);


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: baby_profiles baby_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.baby_profiles
    ADD CONSTRAINT baby_profiles_pkey PRIMARY KEY (id);


--
-- Name: diaper_events diaper_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diaper_events
    ADD CONSTRAINT diaper_events_pkey PRIMARY KEY (id);


--
-- Name: feeding_sessions feeding_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feeding_sessions
    ADD CONSTRAINT feeding_sessions_pkey PRIMARY KEY (id);


--
-- Name: growth_measurements growth_measurements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_measurements
    ADD CONSTRAINT growth_measurements_pkey PRIMARY KEY (id);


--
-- Name: health_events health_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.health_events
    ADD CONSTRAINT health_events_pkey PRIMARY KEY (id);


--
-- Name: sleep_sessions sleep_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sleep_sessions
    ADD CONSTRAINT sleep_sessions_pkey PRIMARY KEY (id);


--
-- Name: diaper_events diaper_events_baby_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diaper_events
    ADD CONSTRAINT diaper_events_baby_id_fkey FOREIGN KEY (baby_id) REFERENCES public.baby_profiles(id);


--
-- Name: feeding_sessions feeding_sessions_baby_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feeding_sessions
    ADD CONSTRAINT feeding_sessions_baby_id_fkey FOREIGN KEY (baby_id) REFERENCES public.baby_profiles(id);


--
-- Name: growth_measurements growth_measurements_baby_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_measurements
    ADD CONSTRAINT growth_measurements_baby_id_fkey FOREIGN KEY (baby_id) REFERENCES public.baby_profiles(id);


--
-- Name: health_events health_events_baby_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.health_events
    ADD CONSTRAINT health_events_baby_id_fkey FOREIGN KEY (baby_id) REFERENCES public.baby_profiles(id);


--
-- Name: sleep_sessions sleep_sessions_baby_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sleep_sessions
    ADD CONSTRAINT sleep_sessions_baby_id_fkey FOREIGN KEY (baby_id) REFERENCES public.baby_profiles(id);


--
-- PostgreSQL database dump complete
--


--
-- Local edit: seed the Alembic head so the CI database records which migration
-- this snapshot was taken at, exactly as the real database does. Nothing in dbt
-- reads it; it is here so a `select * from alembic_version` in CI answers the
-- "which schema is this?" question the same way it does on the server.
--
INSERT INTO public.alembic_version (version_num) VALUES ('e7a91b4c2d58');
