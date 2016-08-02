functions = """
--
-- SQL functions for NIPAP
--

--
-- calc_indent is an internal function that calculates the correct indentation
-- for a prefix. It is called from a trigger function on the ip_net_plan table.
--
CREATE OR REPLACE FUNCTION calc_indent(arg_vrf integer, arg_prefix inet, delta integer) RETURNS bool AS $_$
DECLARE
	r record;
	current_indent integer;
BEGIN
	current_indent := (
		SELECT COUNT(*)
		FROM
			(SELECT DISTINCT inp.prefix
			FROM ip_net_plan inp
			WHERE vrf_id = arg_vrf
				AND iprange(prefix) >> iprange(arg_prefix::cidr)
			) AS a
		);

	UPDATE ip_net_plan SET indent = current_indent WHERE vrf_id = arg_vrf AND prefix = arg_prefix;
	UPDATE ip_net_plan SET indent = indent + delta WHERE vrf_id = arg_vrf AND iprange(prefix) << iprange(arg_prefix::cidr);

	RETURN true;
END;
$_$ LANGUAGE plpgsql;


--
-- Remove duplicate elements from an array
--
CREATE OR REPLACE FUNCTION array_undup(ANYARRAY) RETURNS ANYARRAY AS $_$
	SELECT ARRAY(
		SELECT DISTINCT $1[i]
		FROM generate_series(
			array_lower($1,1),
			array_upper($1,1)
			) AS i
		);
$_$ LANGUAGE SQL;


--
-- calc_tags is an internal function that calculates the inherited_tags
-- from parent prefixes to its children. It is called from a trigger function
-- on the ip_net_plan table.
--
CREATE OR REPLACE FUNCTION calc_tags(arg_vrf integer, arg_prefix inet) RETURNS bool AS $_$
DECLARE
	i_indent integer;
	new_inherited_tags text[];
BEGIN
	-- TODO: why don't we take the prefix as argument? That way we could save
	--		 in these selects fetching data from the table that we already have.
	i_indent := (
		SELECT indent+1
		FROM ip_net_plan
		WHERE vrf_id = arg_vrf
			AND prefix = arg_prefix
		);
	-- set default if we don't have a parent prefix
	IF i_indent IS NULL THEN
		i_indent := 0;
	END IF;

	new_inherited_tags := (
		SELECT array_undup(array_cat(inherited_tags, tags))
		FROM ip_net_plan
		WHERE vrf_id = arg_vrf
			AND prefix = arg_prefix
		);
	-- set default if we don't have a parent prefix
	IF new_inherited_tags IS NULL THEN
		new_inherited_tags := '{}';
	END IF;

	-- TODO: we don't need to update if our old tags are the same as our new
	-- TODO: we could add WHERE inherited_tags != new_inherited_tags which
	--		 could potentially speed up this update considerably, especially
	--		 with a GiN index on that column
	UPDATE ip_net_plan SET inherited_tags = new_inherited_tags WHERE vrf_id = arg_vrf AND iprange(prefix) << iprange(arg_prefix::cidr) AND indent = i_indent;

	RETURN true;
END;
$_$ LANGUAGE plpgsql;



--
-- find free IP ranges within the specified prefix
--------------------------------------------------

--
-- overloaded funciton for feeding array of IDs
--
CREATE OR REPLACE FUNCTION find_free_ranges (IN arg_prefix_ids integer[]) RETURNS SETOF iprange AS $_$
DECLARE
	id int;
	r iprange;
BEGIN
	FOR id IN (SELECT arg_prefix_ids[i] FROM generate_subscripts(arg_prefix_ids, 1) AS i) LOOP
		FOR r IN (SELECT find_free_ranges(id)) LOOP
			RETURN NEXT r;
		END LOOP;
	END LOOP;

	RETURN;
END;
$_$ LANGUAGE plpgsql;

--
-- Each range starts on the first non-used address, ie broadcast of "previous
-- prefix" + 1 and ends on address before network address of "next prefix".
--
CREATE OR REPLACE FUNCTION find_free_ranges (arg_prefix_id integer) RETURNS SETOF iprange AS $_$
DECLARE
	arg_prefix record;
	current_prefix record; -- current prefix
	max_prefix_len integer;
	last_used inet;
BEGIN
	SELECT * INTO arg_prefix FROM ip_net_plan WHERE id = arg_prefix_id;

	IF family(arg_prefix.prefix) = 4 THEN
		max_prefix_len := 32;
	ELSE
		max_prefix_len := 128;
	END IF;

	-- used the network address of the "parent" prefix as start value
	last_used := host(network(arg_prefix.prefix));

	-- loop over direct childrens of arg_prefix
	FOR current_prefix IN (SELECT * FROM ip_net_plan WHERE prefix <<= arg_prefix.prefix AND vrf_id = arg_prefix.vrf_id AND indent = arg_prefix.indent + 1 ORDER BY prefix ASC) LOOP
		-- if network address of current prefix is higher than the last used
		-- address (typically the broadcast address of the previous network) it
		-- means that this and the previous network are not adjacent, ie we
		-- have found a free range, let's return it!
		IF set_masklen(last_used, max_prefix_len)::cidr < set_masklen(current_prefix.prefix, max_prefix_len)::cidr THEN
			RETURN NEXT iprange(last_used::ipaddress, host(network(current_prefix.prefix)-1)::ipaddress);
		END IF;

		-- store current_prefix as last_used for next round
		-- if the current prefix has the max prefix length, the next free address is current_prefix + 1
		IF masklen(current_prefix.prefix) = max_prefix_len THEN
			last_used := current_prefix.prefix + 1;
		-- if broadcast of current prefix is same as the broadcast of
		-- arg_prefix we use that address as the last used, as it's really the max
		ELSIF set_masklen(broadcast(current_prefix.prefix), max_prefix_len) = set_masklen(broadcast(arg_prefix.prefix), max_prefix_len) THEN
			last_used := broadcast(current_prefix.prefix);
		-- default to using broadcast of current_prefix +1
		ELSE
			last_used := broadcast(current_prefix.prefix) + 1;
		END IF;
	END LOOP;

	-- and get the "last free" range if there is one
	IF last_used::ipaddress < set_masklen(broadcast(arg_prefix.prefix), max_prefix_len)::ipaddress THEN
		RETURN NEXT iprange(last_used::ipaddress, set_masklen(broadcast(arg_prefix.prefix), max_prefix_len)::ipaddress);
	END IF;

	RETURN;
END;
$_$ LANGUAGE plpgsql;



--
-- Return aggregated CIDRs based on ip ranges.
--
CREATE OR REPLACE FUNCTION iprange2cidr (IN arg_ipranges iprange[]) RETURNS SETOF cidr AS $_$
DECLARE
	current_range iprange;
	delta numeric(40);
	biggest integer;
	big_prefix iprange;
	rest iprange[]; -- the rest
	free_prefixes cidr[];
	max_prefix_len integer;
	p cidr;
	len integer;
BEGIN
	FOR current_range IN (SELECT arg_ipranges[s] FROM generate_series(1, array_upper(arg_ipranges, 1)) AS s) LOOP
		IF max_prefix_len IS NULL THEN
			IF family(current_range) = 4 THEN
				max_prefix_len := 32;
			ELSE
				max_prefix_len := 128;
			END IF;
		ELSE
			IF (family(current_range) = 4 AND max_prefix_len != 32) OR (family(current_range) = 6 AND max_prefix_len != 128) THEN
				RAISE EXCEPTION 'Search prefixes of inconsistent address-family provided';
			END IF;
		END IF;
	END LOOP;

	FOR current_range IN (SELECT arg_ipranges[s] FROM generate_series(1, array_upper(arg_ipranges, 1)) AS s) LOOP
		-- range is an exact CIDR
		IF current_range::cidr::iprange = current_range THEN
			RETURN NEXT current_range;
			CONTINUE;
		END IF;

		-- get size of network
		delta := upper(current_range) - lower(current_range);
		-- the inverse of 2^x to find largest bit size that fits in this prefix
		biggest := max_prefix_len - floor(log(delta)/log(2));

		-- TODO: benchmark this against an approach that uses set_masklen(lower(current_range)+delta, biggest)
		--FOR len IN (SELECT * FROM generate_series(biggest, max_prefix_len)) LOOP
		--	big_prefix := set_masklen(lower(current_range)::cidr+delta, len);
		--	IF lower(big_prefix) >= lower(current_range) AND upper(big_prefix) <= upper(current_range) THEN
		--		EXIT;
		--	END IF;
		--END LOOP;
		<<potential>>
		FOR len IN (SELECT * FROM generate_series(biggest, max_prefix_len)) LOOP
			big_prefix := set_masklen(lower(current_range)::cidr, len);
			WHILE true LOOP
				IF lower(big_prefix) >= lower(current_range) AND upper(big_prefix) <= upper(current_range) THEN
					EXIT potential;
				END IF;
				big_prefix := set_masklen(broadcast(set_masklen(lower(current_range)::cidr, len))+1, len);
				EXIT WHEN upper(big_prefix) >= upper(current_range);
			END LOOP;
		END LOOP potential;

		-- call ourself recursively with the rest between start of range and the big prefix
		IF lower(big_prefix) > lower(current_range) THEN
			FOR p IN (SELECT * FROM iprange2cidr(ARRAY[ iprange(lower(current_range), lower(big_prefix)-1) ])) LOOP
				RETURN NEXT p;
			END LOOP;
		END IF;
		-- biggest prefix
		RETURN NEXT big_prefix;
		-- call ourself recursively with the rest between end of the big prefix and the end of range
		IF upper(big_prefix) < upper(current_range) THEN
			FOR p IN (SELECT * FROM iprange2cidr(ARRAY[ iprange(upper(big_prefix)+1, upper(current_range)) ])) LOOP
				RETURN NEXT p;
			END LOOP;
		END IF;

	END LOOP;

	RETURN;
END;
$_$ LANGUAGE plpgsql;


--
-- Calculate number of free prefixes in a pool
--
CREATE OR REPLACE FUNCTION calc_pool_free_prefixes(arg_pool_id integer, arg_family integer, arg_new_prefix cidr DEFAULT NULL) RETURNS numeric(40) AS $_$
DECLARE
	pool ip_net_pool;
BEGIN
	SELECT * INTO pool FROM ip_net_pool WHERE id = arg_pool_id;
	RETURN calc_pool_free_prefixes(pool, arg_family, arg_new_prefix);
END;
$_$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION calc_pool_free_prefixes(arg_pool ip_net_pool, arg_family integer, arg_new_prefix cidr DEFAULT NULL) RETURNS numeric(40) AS $_$
DECLARE
	default_prefix_length integer;
	prefix_ids integer[];
BEGIN
	IF arg_family = 4 THEN
		default_prefix_length := arg_pool.ipv4_default_prefix_length;
	ELSE
		default_prefix_length := arg_pool.ipv6_default_prefix_length;
	END IF;

	-- not possible to determine amount of free addresses if the default prefix
	-- length is not set
	IF default_prefix_length IS NULL THEN
		RETURN NULL;
	END IF;

	-- if we don't have any member prefixes, free prefixes will be NULL
	prefix_ids := ARRAY((SELECT id FROM ip_net_plan WHERE pool_id = arg_pool.id AND family(prefix)=arg_family));
	IF array_length(prefix_ids, 1) IS NULL THEN
		RETURN NULL;
	END IF;

	RETURN cidr_count(ARRAY((SELECT iprange2cidr(ARRAY((SELECT find_free_ranges(prefix_ids)))))), default_prefix_length);
END;
$_$ LANGUAGE plpgsql;



--
-- Count the number of prefixes of a certain size that fits in the list of
-- CIDRs
--
-- Example:
--   SELECT cidr_count('{1.0.0.0/24,2.0.0.0/23}', 29);
--    cidr_count
--   ------------
--           384
--
CREATE OR REPLACE FUNCTION cidr_count(IN arg_cidrs cidr[], arg_prefix_length integer) RETURNS numeric(40) AS $_$
DECLARE
	i_family integer;
	max_prefix_len integer;
	num_cidrs numeric(40);
	p record;
	i int;
BEGIN
	num_cidrs := 0;

	-- make sure all provided search_prefixes are of same family
	FOR i IN SELECT generate_subscripts(arg_cidrs, 1) LOOP
		IF i_family IS NULL THEN
			i_family := family(arg_cidrs[i]);
		END IF;

		IF i_family != family(arg_cidrs[i]) THEN
			RAISE EXCEPTION 'Search prefixes of inconsistent address-family provided';
		END IF;
	END LOOP;

	-- determine maximum prefix-length for our family
	IF i_family = 4 THEN
		max_prefix_len := 32;
	ELSE
		max_prefix_len := 128;
	END IF;

	FOR i IN (SELECT masklen(arg_cidrs[s]) FROM generate_subscripts(arg_cidrs, 1) AS s) LOOP
		num_cidrs = num_cidrs + power(2::numeric(40), (arg_prefix_length - i));
	END LOOP;

	RETURN num_cidrs;
END;
$_$ LANGUAGE plpgsql;




--
-- find_free_prefix finds one or more prefix(es) of a certain prefix-length
-- inside a larger prefix. It is typically called by get_prefix or to return a
-- list of unused prefixes.
--

-- default to 1 prefix if no count is specified
CREATE OR REPLACE FUNCTION find_free_prefix(arg_vrf integer, IN arg_prefixes inet[], arg_wanted_prefix_len integer) RETURNS SETOF inet AS $_$
BEGIN
	RETURN QUERY SELECT * FROM find_free_prefix(arg_vrf, arg_prefixes, arg_wanted_prefix_len, 1) AS prefix;
END;
$_$ LANGUAGE plpgsql;

-- full function
CREATE OR REPLACE FUNCTION find_free_prefix(arg_vrf integer, IN arg_prefixes inet[], arg_wanted_prefix_len integer, arg_count integer) RETURNS SETOF inet AS $_$
DECLARE
	i_family integer;
	i_found integer;
	p int;
	search_prefix inet;
	current_prefix inet;
	max_prefix_len integer;
	covering_prefix inet;
BEGIN
	covering_prefix := NULL;
	-- sanity checking
	-- make sure all provided search_prefixes are of same family
	FOR p IN SELECT generate_subscripts(arg_prefixes, 1) LOOP
		IF i_family IS NULL THEN
			i_family := family(arg_prefixes[p]);
		END IF;

		IF i_family != family(arg_prefixes[p]) THEN
			RAISE EXCEPTION 'Search prefixes of inconsistent address-family provided';
		END IF;
	END LOOP;

	-- determine maximum prefix-length for our family
	IF i_family = 4 THEN
		max_prefix_len := 32;
	ELSE
		max_prefix_len := 128;
	END IF;

	-- the wanted prefix length cannot be more than 32 for ipv4 or more than 128 for ipv6
	IF arg_wanted_prefix_len > max_prefix_len THEN
		RAISE EXCEPTION 'Requested prefix-length exceeds max prefix-length %', max_prefix_len;
	END IF;
	--

	i_found := 0;

	-- loop through our search list of prefixes
	FOR p IN SELECT generate_subscripts(arg_prefixes, 1) LOOP
		-- save the current prefix in which we are looking for a candidate
		search_prefix := arg_prefixes[p];

		IF (masklen(search_prefix) > arg_wanted_prefix_len) THEN
			CONTINUE;
		END IF;

		SELECT set_masklen(search_prefix, arg_wanted_prefix_len) INTO current_prefix;

		-- we step through our search_prefix in steps of the wanted prefix
		-- length until we are beyond the broadcast size, ie end of our
		-- search_prefix
		WHILE set_masklen(current_prefix, masklen(search_prefix)) <= broadcast(search_prefix) LOOP
			-- tests put in order of speed, fastest one first

			-- the following are address family agnostic
			IF current_prefix IS NULL THEN
				SELECT broadcast(current_prefix) + 1 INTO current_prefix;
				CONTINUE;
			END IF;
			IF EXISTS (SELECT 1 FROM ip_net_plan WHERE vrf_id = arg_vrf AND prefix = current_prefix) THEN
				SELECT broadcast(current_prefix) + 1 INTO current_prefix;
				CONTINUE;
			END IF;

			-- avoid prefixes larger than the current_prefix but inside our search_prefix
			covering_prefix := (SELECT prefix FROM ip_net_plan WHERE vrf_id = arg_vrf AND iprange(prefix) >>= iprange(current_prefix::cidr) AND iprange(prefix) << iprange(search_prefix::cidr) ORDER BY masklen(prefix) ASC LIMIT 1);
			IF covering_prefix IS NOT NULL THEN
				SELECT set_masklen(broadcast(covering_prefix) + 1, arg_wanted_prefix_len) INTO current_prefix;
				CONTINUE;
			END IF;

			-- prefix must not contain any breakouts, that would mean it's not empty, ie not free
			IF EXISTS (SELECT 1 FROM ip_net_plan WHERE vrf_id = arg_vrf AND iprange(prefix) <<= iprange(current_prefix::cidr)) THEN
				SELECT broadcast(current_prefix) + 1 INTO current_prefix;
				CONTINUE;
			END IF;

			-- while the following two tests are family agnostic, they use
			-- functions and so are not indexed
			-- TODO: should they be indexed?

			IF ((i_family = 4 AND masklen(search_prefix) < 31) OR i_family = 6 AND masklen(search_prefix) < 127)THEN
				IF (set_masklen(network(search_prefix), max_prefix_len) = current_prefix) THEN
					SELECT broadcast(current_prefix) + 1 INTO current_prefix;
					CONTINUE;
				END IF;
				IF (set_masklen(broadcast(search_prefix), max_prefix_len) = current_prefix) THEN
					SELECT broadcast(current_prefix) + 1 INTO current_prefix;
					CONTINUE;
				END IF;
			END IF;

			RETURN NEXT current_prefix;

			i_found := i_found + 1;
			IF i_found >= arg_count THEN
				RETURN;
			END IF;

			current_prefix := broadcast(current_prefix) + 1;
		END LOOP;

	END LOOP;

	RETURN;

END;
$_$ LANGUAGE plpgsql;



--
-- get_prefix provides a convenient and MVCC-proof way of getting the next
-- available prefix from another prefix.
--
CREATE OR REPLACE FUNCTION get_prefix(arg_vrf integer, IN arg_prefixes inet[], arg_wanted_prefix_len integer) RETURNS inet AS $_$
DECLARE
	p inet;
BEGIN
	LOOP
		-- get a prefix
		SELECT prefix INTO p FROM find_free_prefix(arg_vrf, arg_prefixes, arg_wanted_prefix_len) AS prefix;

		BEGIN
			INSERT INTO ip_net_plan (vrf_id, prefix) VALUES (arg_vrf, p);
			RETURN p;
		EXCEPTION WHEN unique_violation THEN
			-- Loop and try to find a new prefix
		END;

	END LOOP;
END;
$_$ LANGUAGE plpgsql;



--
-- Helper to sort VRF RTs
--
-- RTs are tricky to sort since they exist in two formats and have the classic
-- sorted-as-string-problem;
--
--      199:456
--     1234:456
--  1.3.3.7:456
--
CREATE OR REPLACE FUNCTION vrf_rt_order(arg_rt text) RETURNS bigint AS $_$
DECLARE
	part_one text;
	part_two text;
	ip text;
BEGIN
	BEGIN
		part_one := split_part(arg_rt, ':', 1)::bigint;
	EXCEPTION WHEN others THEN
		ip := split_part(arg_rt, ':', 1);
		part_one := (split_part(ip, '.', 1)::bigint << 24) +
					(split_part(ip, '.', 2)::bigint << 16) +
					(split_part(ip, '.', 3)::bigint << 8) +
					(split_part(ip, '.', 4)::bigint);
	END;

	part_two := split_part(arg_rt, ':', 2);

	RETURN (part_one::bigint << 32) + part_two::bigint;
END;
$_$ LANGUAGE plpgsql IMMUTABLE STRICT;
"""

ip_net = """
--------------------------------------------
--
-- The basic table structure and similar
--
--------------------------------------------

COMMENT ON DATABASE nipap IS 'NIPAP database - schema version: 6';

CREATE EXTENSION IF NOT EXISTS ip4r;
CREATE EXTENSION IF NOT EXISTS hstore;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TYPE ip_net_plan_type AS ENUM ('reservation', 'assignment', 'host');
CREATE TYPE ip_net_plan_status AS ENUM ('assigned', 'reserved', 'quarantine');

CREATE TYPE priority_5step AS ENUM ('warning', 'low', 'medium', 'high', 'critical');


CREATE TABLE ip_net_asn (
	asn integer NOT NULL PRIMARY KEY,
	name text
);

COMMENT ON COLUMN ip_net_asn.asn IS 'AS Number';
COMMENT ON COLUMN ip_net_asn.name IS 'ASN name';

--
-- This is where we store VRFs
--
CREATE TABLE ip_net_vrf (
	id serial PRIMARY KEY,
	rt text,
	name text NOT NULL,
	description text,
	num_prefixes_v4 numeric(40) DEFAULT 0,
	num_prefixes_v6 numeric(40) DEFAULT 0,
	total_addresses_v4 numeric(40) DEFAULT 0,
	total_addresses_v6 numeric(40) DEFAULT 0,
	used_addresses_v4 numeric(40) DEFAULT 0,
	used_addresses_v6 numeric(40) DEFAULT 0,
	free_addresses_v4 numeric(40) DEFAULT 0,
	free_addresses_v6 numeric(40) DEFAULT 0,
	tags text[] DEFAULT '{}',
	avps hstore NOT NULL DEFAULT ''
);

--
-- A little hack to allow a single VRF with no VRF or name
--
CREATE UNIQUE INDEX ip_net_vrf__unique_vrf__index ON ip_net_vrf ((''::TEXT)) WHERE rt IS NULL;
CREATE UNIQUE INDEX ip_net_vrf__unique_name__index ON ip_net_vrf ((''::TEXT)) WHERE name IS NULL;
--
INSERT INTO ip_net_vrf (id, rt, name, description) VALUES (0, NULL, 'default', 'The default VRF, typically the Internet.');

CREATE UNIQUE INDEX ip_net_vrf__rt__index ON ip_net_vrf (rt) WHERE rt IS NOT NULL;
CREATE UNIQUE INDEX ip_net_vrf__name__index ON ip_net_vrf (lower(name)) WHERE name IS NOT NULL;

COMMENT ON TABLE ip_net_vrf IS 'IP Address VRFs';
COMMENT ON INDEX ip_net_vrf__rt__index IS 'VRF RT';
COMMENT ON INDEX ip_net_vrf__name__index IS 'VRF name';
COMMENT ON COLUMN ip_net_vrf.rt IS 'VRF RT';
COMMENT ON COLUMN ip_net_vrf.name IS 'VRF name';
COMMENT ON COLUMN ip_net_vrf.description IS 'VRF description';
COMMENT ON COLUMN ip_net_vrf.num_prefixes_v4 IS 'Number of IPv4 prefixes in this VRF';
COMMENT ON COLUMN ip_net_vrf.num_prefixes_v6 IS 'Number of IPv6 prefixes in this VRF';
COMMENT ON COLUMN ip_net_vrf.total_addresses_v4 IS 'Total number of IPv4 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.total_addresses_v6 IS 'Total number of IPv6 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.used_addresses_v4 IS 'Number of used IPv4 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.used_addresses_v6 IS 'Number of used IPv6 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.free_addresses_v4 IS 'Number of free IPv4 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.free_addresses_v6 IS 'Number of free IPv6 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.tags IS 'Tags associated with the VRF';



--
-- This table is used to store our pools. pools are the grouping of a number of
-- prefixes for a specific purpose and when you need a specific type of
-- address, ie a core loopback or similar, you'll just pick the right pool and
-- get an address assigned automatically.
--
CREATE TABLE ip_net_pool (
	id serial PRIMARY KEY,
	name text NOT NULL,
	description text,
	default_type ip_net_plan_type,
	ipv4_default_prefix_length integer,
	ipv6_default_prefix_length integer,
	member_prefixes_v4 numeric(40) DEFAULT 0,
	member_prefixes_v6 numeric(40) DEFAULT 0,
	used_prefixes_v4 numeric(40) DEFAULT 0,
	used_prefixes_v6 numeric(40) DEFAULT 0,
	total_addresses_v4 numeric(40) DEFAULT 0,
	total_addresses_v6 numeric(40) DEFAULT 0,
	used_addresses_v4 numeric(40) DEFAULT 0,
	used_addresses_v6 numeric(40) DEFAULT 0,
	free_addresses_v4 numeric(40) DEFAULT 0,
	free_addresses_v6 numeric(40) DEFAULT 0,
	free_prefixes_v4 numeric(40) DEFAULT NULL,
	free_prefixes_v6 numeric(40) DEFAULT NULL,
	total_prefixes_v4 numeric(40) DEFAULT NULL,
	total_prefixes_v6 numeric(40) DEFAULT NULL,
	tags text[] DEFAULT '{}',
	avps hstore NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX ip_net_pool__name__index ON ip_net_pool (lower(name));

COMMENT ON TABLE ip_net_pool IS 'IP Pools for assigning prefixes from';

COMMENT ON INDEX ip_net_pool__name__index IS 'pool name';

COMMENT ON COLUMN ip_net_pool.id IS 'Unique ID of pool';
COMMENT ON COLUMN ip_net_pool.name IS 'Pool name';
COMMENT ON COLUMN ip_net_pool.description IS 'Pool description';
COMMENT ON COLUMN ip_net_pool.default_type IS 'Default type for prefix allocated from pool';
COMMENT ON COLUMN ip_net_pool.ipv4_default_prefix_length IS 'Default prefix-length for IPv4 prefix allocated from pool';
COMMENT ON COLUMN ip_net_pool.ipv6_default_prefix_length IS 'Default prefix-length for IPv6 prefix allocated from pool';
COMMENT ON COLUMN ip_net_pool.member_prefixes_v4 IS 'Number of IPv4 prefixes that are members of this pool';
COMMENT ON COLUMN ip_net_pool.member_prefixes_v6 IS 'Number of IPv6 prefixes that are members of this pool';
COMMENT ON COLUMN ip_net_pool.used_prefixes_v4 IS 'Number of IPv4 prefixes allocated from this pool';
COMMENT ON COLUMN ip_net_pool.used_prefixes_v6 IS 'Number of IPv6 prefixes allocated from this pool';
COMMENT ON COLUMN ip_net_pool.total_addresses_v4 IS 'Total number of IPv4 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.total_addresses_v6 IS 'Total number of IPv6 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.used_addresses_v4 IS 'Number of used IPv4 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.used_addresses_v6 IS 'Number of used IPv6 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.free_addresses_v4 IS 'Number of free IPv4 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.free_addresses_v6 IS 'Number of free IPv6 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.free_prefixes_v4 IS 'Number of potentially free IPv4 prefixes of the default assignment size';
COMMENT ON COLUMN ip_net_pool.free_prefixes_v6 IS 'Number of potentially free IPv6 prefixes of the default assignment size';
COMMENT ON COLUMN ip_net_pool.total_prefixes_v4 IS 'Potentially the total number of IPv4 child prefixes in pool. This is based on current number of childs and potential childs of the default assignment size, which is why it can vary.';
COMMENT ON COLUMN ip_net_pool.total_prefixes_v6 IS 'Potentially the total number of IPv6 child prefixes in pool. This is based on current number of childs and potential childs of the default assignment size, which is why it can vary.';
COMMENT ON COLUMN ip_net_pool.tags IS 'Tags associated with the pool';



--
-- this table stores the actual prefixes in the address plan, or net 
-- plan as I prefer to call it
--
-- pool is the pool for which this prefix is part of and from which 
-- assignments can be made
--
CREATE TABLE ip_net_plan (
	id serial PRIMARY KEY,
	vrf_id integer NOT NULL DEFAULT 0 REFERENCES ip_net_vrf (id) ON UPDATE CASCADE ON DELETE CASCADE,
	prefix cidr NOT NULL,
	display_prefix inet,
	description text,
	comment text,
	node text,
	pool_id integer REFERENCES ip_net_pool (id) ON UPDATE CASCADE ON DELETE SET NULL,
	type ip_net_plan_type NOT NULL,
	indent integer,
	country text,
	order_id text,
	customer_id text,
	external_key text,
	authoritative_source text NOT NULL DEFAULT 'nipap',
	alarm_priority priority_5step,
	monitor boolean,
	children integer,
	vlan integer,
	tags text[] DEFAULT '{}',
	inherited_tags text[] DEFAULT '{}',
	added timestamp with time zone DEFAULT NOW(),
	last_modified timestamp with time zone DEFAULT NOW(),
	total_addresses numeric(40),
	used_addresses numeric(40),
	free_addresses numeric(40),
	status ip_net_plan_status NOT NULL DEFAULT 'assigned',
	avps hstore NOT NULL DEFAULT '',
	expires timestamp with time zone DEFAULT 'infinity'
);

COMMENT ON TABLE ip_net_plan IS 'Actual address / prefix plan';

COMMENT ON COLUMN ip_net_plan.vrf_id IS 'VRF in which the prefix resides';
COMMENT ON COLUMN ip_net_plan.prefix IS '"true" IP prefix, with hosts registered as /32';
COMMENT ON COLUMN ip_net_plan.display_prefix IS 'IP prefix with hosts having their covering assignments prefix-length';
COMMENT ON COLUMN ip_net_plan.description IS 'Prefix description';
COMMENT ON COLUMN ip_net_plan.comment IS 'Comment!';
COMMENT ON COLUMN ip_net_plan.node IS 'Name of the node, typically the hostname or FQDN of the node (router/switch/host) on which the address is configured';
COMMENT ON COLUMN ip_net_plan.pool_id IS 'Pool that this prefix is part of';
COMMENT ON COLUMN ip_net_plan.type IS 'Type is one of "reservation", "assignment" or "host"';
COMMENT ON COLUMN ip_net_plan.indent IS 'Number of indents to properly render this prefix';
COMMENT ON COLUMN ip_net_plan.country IS 'ISO3166-1 two letter country code';
COMMENT ON COLUMN ip_net_plan.order_id IS 'Order identifier';
COMMENT ON COLUMN ip_net_plan.customer_id IS 'Customer identifier';
COMMENT ON COLUMN ip_net_plan.external_key IS 'Field for use by exernal systems which need references to its own dataset.';
COMMENT ON COLUMN ip_net_plan.authoritative_source IS 'The authoritative source for information regarding this prefix';
COMMENT ON COLUMN ip_net_plan.alarm_priority IS 'Priority of alarms sent for this prefix to NetWatch.';
COMMENT ON COLUMN ip_net_plan.monitor IS 'Whether the prefix should be monitored or not.';
COMMENT ON COLUMN ip_net_plan.children IS 'Number of direct sub-prefixes';
COMMENT ON COLUMN ip_net_plan.vlan IS 'VLAN ID';
COMMENT ON COLUMN ip_net_plan.tags IS 'Tags associated with the prefix';
COMMENT ON COLUMN ip_net_plan.inherited_tags IS 'Tags inherited from parent (and grand-parent) prefixes';
COMMENT ON COLUMN ip_net_plan.added IS 'The date and time when the prefix was added';
COMMENT ON COLUMN ip_net_plan.last_modified IS 'The date and time when the prefix was last modified';
COMMENT ON COLUMN ip_net_plan.total_addresses IS 'Total number of addresses in this prefix';
COMMENT ON COLUMN ip_net_plan.used_addresses IS 'Number of used addresses in this prefix';
COMMENT ON COLUMN ip_net_plan.free_addresses IS 'Number of free addresses in this prefix';
COMMENT ON COLUMN ip_net_plan.avps IS 'Extra values / AVPs (Attribute Value Pairs)';
COMMENT ON COLUMN ip_net_plan.expires IS 'Expire time of prefix';

CREATE UNIQUE INDEX ip_net_plan__vrf_id_prefix__index ON ip_net_plan (vrf_id, prefix);

CREATE INDEX ip_net_plan__vrf_id__index ON ip_net_plan (vrf_id);
CREATE INDEX ip_net_plan__node__index ON ip_net_plan (node);
CREATE INDEX ip_net_plan__family__index ON ip_net_plan (family(prefix));
CREATE INDEX ip_net_plan__prefix_iprange_index ON ip_net_plan USING gist(iprange(prefix));
CREATE INDEX ip_net_plan__pool_id__index ON ip_net_plan (pool_id);

COMMENT ON INDEX ip_net_plan__vrf_id_prefix__index IS 'prefix';

--
-- Audit log table
--
CREATE TABLE ip_net_log (
	id serial PRIMARY KEY,
	vrf_id INTEGER,
	vrf_rt TEXT,
	vrf_name TEXT,
	prefix_prefix cidr,
	prefix_id INTEGER,
	pool_name TEXT,
	pool_id INTEGER,
	timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
	username TEXT NOT NULL,
	authenticated_as TEXT NOT NULL,
	authoritative_source TEXT NOT NULL,
	full_name TEXT,
	description TEXT NOT NULL
);

COMMENT ON TABLE ip_net_log IS 'Log of changes made to tables';

COMMENT ON COLUMN ip_net_log.vrf_id IS 'ID of affected VRF, or VRF of affected prefix';
COMMENT ON COLUMN ip_net_log.vrf_rt IS 'RT of affected VRF, or RT of VRF of affected prefix';
COMMENT ON COLUMN ip_net_log.vrf_name IS 'Name of affected VRF, or name of VRF of affected prefix';
COMMENT ON COLUMN ip_net_log.prefix_id IS 'ID of affected prefix';
COMMENT ON COLUMN ip_net_log.prefix_prefix IS 'Prefix which was affected of the action';
COMMENT ON COLUMN ip_net_log.pool_id IS 'ID of affected pool';
COMMENT ON COLUMN ip_net_log.pool_name IS 'Name of affected pool';
COMMENT ON COLUMN ip_net_log.timestamp IS 'Time when the change was made';
COMMENT ON COLUMN ip_net_log.username IS 'Username of the user who made the change';
COMMENT ON COLUMN ip_net_log.authenticated_as IS 'Username of user who authenticated the change. This can be a real person or a system which is trusted to perform operations in another users name.';
COMMENT ON COLUMN ip_net_log.authoritative_source IS 'System from which the action was made';
COMMENT ON COLUMN ip_net_log.full_name IS 'Full name of the user who is responsible for the action';
COMMENT ON COLUMN ip_net_log.description IS 'Text describing the action';

--
-- Indices.
--
CREATE INDEX ip_net_log__vrf__index ON ip_net_log(vrf_id);
CREATE INDEX ip_net_log__prefix__index ON ip_net_log(prefix_id);
CREATE INDEX ip_net_log__pool__index ON ip_net_log(pool_id);

"""

triggers = """
--
-- SQL triggers and trigger functions for NIPAP
--

--
-- Trigger function to validate VRF input, prominently the RT attribute which
-- needs to follow the allowed formats
--
CREATE OR REPLACE FUNCTION tf_ip_net_vrf_iu_before() RETURNS trigger AS $_$
DECLARE
	rt_part_one text;
	rt_part_two text;
	ip text;
	rt_style text;
BEGIN
	-- don't allow setting an RT for VRF id 0
	IF NEW.id = 0 THEN
		IF NEW.rt IS NOT NULL THEN
			RAISE EXCEPTION 'Invalid input for column rt, must be NULL for VRF id 0';
		END IF;
	ELSE -- make sure all VRF except for VRF id 0 has a proper RT
		-- make sure we only have two fields delimited by a colon
		IF (SELECT COUNT(1) FROM regexp_matches(NEW.rt, '(:)', 'g')) != 1 THEN
			RAISE EXCEPTION '1200:Invalid input for column rt, should be ASN:id (123:456) or IP:id (1.3.3.7:456)';
		END IF;

		-- determine RT style, ie 123:456 or IP:id
		BEGIN
			-- either it's a integer (AS number)
			rt_part_one := split_part(NEW.rt, ':', 1)::bigint;
			rt_style := 'simple';
		EXCEPTION WHEN others THEN
			rt_style := 'ip';
		END;

		-- second part
		BEGIN
			rt_part_two := split_part(NEW.rt, ':', 2)::bigint;
		EXCEPTION WHEN others THEN
			RAISE EXCEPTION '1200:Invalid input for column rt, should be ASN:id (123:456) or IP:id (1.3.3.7:456)';
		END;

		-- first part
		IF rt_style = 'simple' THEN
			BEGIN
				rt_part_one := split_part(NEW.rt, ':', 1)::bigint;
			EXCEPTION WHEN others THEN
				RAISE EXCEPTION '1200:1Invalid input for column rt, should be ASN:id (123:456) or IP:id (1.3.3.7:456)';
			END;

			-- reconstruct value
			NEW.rt := rt_part_one::text || ':' || rt_part_two::text;

		ELSIF rt_style = 'ip' THEN
			BEGIN
				-- or an IPv4 address
				ip := host(split_part(NEW.rt, ':', 1)::inet);
				rt_part_one := (split_part(ip, '.', 1)::bigint << 24) +
							(split_part(ip, '.', 2)::bigint << 16) +
							(split_part(ip, '.', 3)::bigint << 8) +
							(split_part(ip, '.', 4)::bigint);
			EXCEPTION WHEN others THEN
				RAISE EXCEPTION '1200:Invalid input for column rt, should be ASN:id (123:456) or IP:id (1.3.3.7:456)';
			END;

			-- reconstruct IP value
			NEW.rt := (split_part(ip, '.', 1)::bigint) || '.' ||
							(split_part(ip, '.', 2)::bigint) || '.' ||
							(split_part(ip, '.', 3)::bigint) || '.' ||
							(split_part(ip, '.', 4)::bigint) || ':' ||
							rt_part_two::text;
		ELSE
			RAISE EXCEPTION '1200:Invalid input for column rt, should be ASN:id (123:456) or IP:id (1.3.3.7:456)';
		END IF;
	END IF;

	RETURN NEW;
END;
$_$ LANGUAGE plpgsql;


--
-- Trigger function to keep data consistent in the ip_net_vrf table with
-- regards to prefix type and similar. This function handles DELETE operations.
--
CREATE OR REPLACE FUNCTION tf_ip_net_vrf_d_before() RETURNS trigger AS $_$
BEGIN
	-- block delete of default VRF with id 0
	IF OLD.id = 0 THEN
		RAISE EXCEPTION '1200:Prohibited delete of default VRF (id=0).';
	END IF;

	RETURN OLD;
END;
$_$ LANGUAGE plpgsql;


--
-- Trigger function to keep data consistent in the ip_net_plan table with
-- regards to prefix type and similar. This function handles INSERTs and
-- UPDATEs.
--
CREATE OR REPLACE FUNCTION tf_ip_net_plan__prefix_iu_before() RETURNS trigger AS $_$
DECLARE
	new_parent RECORD;
	child RECORD;
	i_max_pref_len integer;
	p RECORD;
	num_used numeric(40);
BEGIN
	-- this is a shortcut to avoid running the rest of this trigger as it
	-- can be fairly costly performance wise
	--
	-- sanity checking is done on 'type' and derivations of 'prefix' so if
	-- those stay the same, we don't need to run the rest of the sanity
	-- checks.
	IF TG_OP = 'UPDATE' THEN
		-- don't allow changing VRF
		IF OLD.vrf_id != NEW.vrf_id THEN
			RAISE EXCEPTION '1200:Changing VRF is not allowed';
		END IF;

		-- update last modified timestamp
		NEW.last_modified = NOW();

		-- if vrf, type and prefix is the same, quick return!
		IF OLD.vrf_id = NEW.vrf_id AND OLD.type = NEW.type AND OLD.prefix = NEW.prefix THEN
			RETURN NEW;
		END IF;
	END IF;


	i_max_pref_len := 32;
	IF family(NEW.prefix) = 6 THEN
		i_max_pref_len := 128;
	END IF;
	-- contains the parent prefix
	IF TG_OP = 'INSERT' THEN
		SELECT * INTO new_parent FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND iprange(prefix) >> iprange(NEW.prefix) ORDER BY masklen(prefix) DESC LIMIT 1;
	ELSE
		-- avoid selecting our old self as parent
		SELECT * INTO new_parent FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND iprange(prefix) >> iprange(NEW.prefix) AND prefix != OLD.prefix ORDER BY masklen(prefix) DESC LIMIT 1;
	END IF;

	--
	---- Various sanity checking -----------------------------------------------
	--
	-- Trigger on: vrf_id, prefix, type
	--
	-- check that type is correct on insert and update
	IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
		IF NEW.type = 'host' THEN
			IF masklen(NEW.prefix) != i_max_pref_len THEN
				RAISE EXCEPTION '1200:Prefix of type host must have all bits set in netmask';
			END IF;
			IF new_parent.prefix IS NULL THEN
				RAISE EXCEPTION '1200:Prefix of type host must have a parent (covering) prefix of type assignment';
			END IF;
			IF new_parent.type != 'assignment' THEN
				RAISE EXCEPTION '1200:Parent prefix (%) is of type % but must be of type ''assignment''', new_parent.prefix, new_parent.type;
			END IF;
			NEW.display_prefix := set_masklen(NEW.prefix::inet, masklen(new_parent.prefix));

		ELSIF NEW.type = 'assignment' THEN
			IF new_parent.type IS NULL THEN
				-- all good
			ELSIF new_parent.type != 'reservation' THEN
				RAISE EXCEPTION '1200:Parent prefix (%) is of type % but must be of type ''reservation''', new_parent.prefix, new_parent.type;
			END IF;

			-- also check that the new prefix does not have any childs other than hosts
			--
			-- need to separate INSERT and UPDATE as OLD (which we rely on in
			-- the update case) is not set for INSERT queries
			IF TG_OP = 'INSERT' THEN
				IF EXISTS (SELECT * FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND type != 'host' AND iprange(prefix) << iprange(NEW.prefix) LIMIT 1) THEN
					RAISE EXCEPTION '1200:Prefix of type ''assignment'' must not have any subnets other than of type ''host''';
				END IF;
			ELSIF TG_OP = 'UPDATE' THEN
				IF EXISTS (SELECT * FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND type != 'host' AND iprange(prefix) << iprange(NEW.prefix) AND prefix != OLD.prefix LIMIT 1) THEN
					RAISE EXCEPTION '1200:Prefix of type ''assignment'' must not have any subnets other than of type ''host''';
				END IF;
			END IF;
			NEW.display_prefix := NEW.prefix;

		ELSIF NEW.type = 'reservation' THEN
			IF new_parent.type IS NULL THEN
				-- all good
			ELSIF new_parent.type != 'reservation' THEN
				RAISE EXCEPTION '1200:Parent prefix (%) is of type % but must be of type ''reservation''', new_parent.prefix, new_parent.type;
			END IF;
			NEW.display_prefix := NEW.prefix;

		ELSE
			RAISE EXCEPTION '1200:Unknown prefix type';
		END IF;

		-- is the new prefix part of a pool?
		IF NEW.pool_id IS NOT NULL THEN
			-- if so, make sure all prefixes in that pool belong to the same VRF
			IF NEW.vrf_id != (SELECT vrf_id FROM ip_net_plan WHERE pool_id = NEW.pool_id LIMIT 1) THEN
				RAISE EXCEPTION '1200:Change not allowed. All member prefixes of a pool must be in a the same VRF.';
			END IF;
		END IF;

		-- Only allow setting node on prefixes of type host or typ assignment
		-- and when the prefix length is the maximum prefix length for the
		-- address family. The case for assignment is when a /32 is used as a
		-- loopback address or similar in which case it is registered as an
		-- assignment and should be able to have a node specified.
		IF NEW.node IS NOT NULL THEN
			IF NEW.type = 'host' THEN
				-- all good
			ELSIF NEW.type = 'reservation' THEN
				RAISE EXCEPTION '1200:Not allowed to set ''node'' value for prefixes of type ''reservation''.';
			ELSE
				-- not a /32 or /128, so do not allow
				IF masklen(NEW.prefix) != i_max_pref_len THEN
					RAISE EXCEPTION '1200:Not allowed to set ''node'' value for prefixes of type ''assignment'' which do not have all bits set in netmask.';
				END IF;
			END IF;
		END IF;
	END IF;

	-- only allow specific cases for changing the type of prefix
	IF TG_OP = 'UPDATE' THEN
		IF (OLD.type = 'reservation' AND NEW.type = 'assignment') OR (OLD.type = 'assignment' AND new.type = 'reservation') THEN
			-- don't allow any childs, since they would automatically be of the
			-- wrong type, ie inconsistent data
			IF EXISTS (SELECT 1 FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND iprange(prefix) << iprange(NEW.prefix)) THEN
				RAISE EXCEPTION '1200:Changing from type ''%'' to ''%'' requires there to be no child prefixes.', OLD.type, NEW.type;
			END IF;
		ELSE
			IF OLD.type != NEW.type THEN
				RAISE EXCEPTION '1200:Changing type is not allowed';
			END IF;
		END IF;
	END IF;


	--
	---- Calculate indent for new prefix ---------------------------------------
	--
	-- Trigger on: vrf_id, prefix
	--
	-- use parent prefix indent+1 or if parent is not set, it means we are a
	-- top level prefix and we use indent 0
	NEW.indent := COALESCE(new_parent.indent+1, 0);


	--
	---- Statistics ------------------------------------------------------------
	--
	-- Trigger on: vrf_id, prefix
	--

	-- total addresses
	IF family(NEW.prefix) = 4 THEN
		NEW.total_addresses = power(2::numeric, 32 - masklen(NEW.prefix));
	ELSE
		NEW.total_addresses = power(2::numeric, 128 - masklen(NEW.prefix));
	END IF;

	-- used addresses
	-- special case for hosts
	IF masklen(NEW.prefix) = i_max_pref_len THEN
		NEW.used_addresses := NEW.total_addresses;
	ELSE
		num_used := 0;
		IF TG_OP = 'INSERT' THEN
			FOR p IN (SELECT * FROM ip_net_plan WHERE prefix << NEW.prefix AND vrf_id = NEW.vrf_id AND indent = COALESCE(new_parent.indent+1, 0) ORDER BY prefix ASC) LOOP
				num_used := num_used + (SELECT power(2::numeric, i_max_pref_len-masklen(p.prefix)))::numeric(39);
			END LOOP;
		ELSIF TG_OP = 'UPDATE' THEN
			IF OLD.prefix = NEW.prefix THEN
				-- NOOP
			ELSIF NEW.prefix << OLD.prefix AND OLD.indent = NEW.indent THEN -- NEW is smaller and covered by OLD
				FOR p IN (SELECT * FROM ip_net_plan WHERE prefix << NEW.prefix AND vrf_id = NEW.vrf_id AND indent = NEW.indent+1 ORDER BY prefix ASC) LOOP
					num_used := num_used + (SELECT power(2::numeric, i_max_pref_len-masklen(p.prefix)))::numeric(39);
				END LOOP;
			ELSIF NEW.prefix << OLD.prefix THEN -- NEW is smaller and covered by OLD
				--
				FOR p IN (SELECT * FROM ip_net_plan WHERE prefix << NEW.prefix AND vrf_id = NEW.vrf_id AND indent = NEW.indent ORDER BY prefix ASC) LOOP
					num_used := num_used + (SELECT power(2::numeric, i_max_pref_len-masklen(p.prefix)))::numeric(39);
				END LOOP;
			ELSIF NEW.prefix >> OLD.prefix AND OLD.indent = NEW.indent THEN -- NEW is larger and covers OLD, but same indent
				-- since the new prefix covers the old prefix but the indent
				-- hasn't been updated yet, we will see child prefixes with
				-- OLD.indent + 1 and then the part that is now covered by
				-- NEW.prefix but wasn't covered by OLD.prefix will have
				-- indent = NEW.indent ( to be NEW.indent+1 after update)
				FOR p IN (SELECT * FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND prefix != OLD.prefix AND ((indent = OLD.indent+1 AND prefix << OLD.prefix) OR indent = NEW.indent AND prefix << NEW.prefix) ORDER BY prefix ASC) LOOP
					num_used := num_used + (SELECT power(2::numeric, i_max_pref_len-masklen(p.prefix)))::numeric(39);
				END LOOP;

			ELSIF NEW.prefix >> OLD.prefix THEN -- NEW is larger and covers OLD but with different indent
				FOR p IN (SELECT * FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND prefix != OLD.prefix AND (indent = NEW.indent AND prefix << NEW.prefix) ORDER BY prefix ASC) LOOP
					num_used := num_used + (SELECT power(2::numeric, i_max_pref_len-masklen(p.prefix)))::numeric(39);
				END LOOP;
			ELSE -- prefix has been moved and doesn't cover or is covered by OLD
				FOR p IN (SELECT * FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND prefix << NEW.prefix AND indent = COALESCE(new_parent.indent+1, 0) ORDER BY prefix ASC) LOOP
					num_used := num_used + (SELECT power(2::numeric, i_max_pref_len-masklen(p.prefix)))::numeric(39);
				END LOOP;

			END IF;
		END IF;
		NEW.used_addresses = num_used;
	END IF;

	-- free addresses
	NEW.free_addresses := NEW.total_addresses - NEW.used_addresses;


	--
	---- Inherited Tags --------------------------------------------------------
	-- Update inherited tags
	--
	-- Trigger: vrf_id, prefix
	--
	-- set new inherited_tags based on parent_prefix tags and inherited_tags
	IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
		NEW.inherited_tags := array_undup(array_cat(new_parent.inherited_tags, new_parent.tags));
	END IF;


	-- all is well, return
	RETURN NEW;
END;
$_$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION tf_ip_net_plan__other__iu_before() RETURNS trigger AS $_$
DECLARE
BEGIN
	-- Check country code- value needs to be a two letter country code
	-- according to ISO 3166-1 alpha-2
	--
	-- We do not check that the actual value is in ISO 3166-1, because that
	-- would entail including a full listing of country codes which we do not want
	-- as we risk including an outdated one. We don't want to force users to
	-- upgrade merely to get a new ISO 3166-1 list.
	NEW.country = upper(NEW.country);
	IF NEW.country !~ '^[A-Z]{2}$' THEN
		RAISE EXCEPTION '1200: Please enter a two letter country code according to ISO 3166-1 alpha-2';
	END IF;

	RETURN NEW;
END;
$_$ LANGUAGE plpgsql;


--
-- Trigger function to keep data consistent in the ip_net_plan table with
-- regards to prefix type and similar. This function handles DELETE operations.
--
CREATE OR REPLACE FUNCTION tf_ip_net_prefix_d_before() RETURNS trigger AS $_$
BEGIN
	-- if an assignment contains hosts, we block the delete
	IF OLD.type = 'assignment' THEN
		-- contains one child prefix
		IF (SELECT COUNT(1) FROM ip_net_plan WHERE iprange(prefix) << iprange(OLD.prefix) AND vrf_id = OLD.vrf_id LIMIT 1) > 0 THEN
			RAISE EXCEPTION '1200:Prohibited delete, prefix (%) contains hosts.', OLD.prefix;
		END IF;
	END IF;

	RETURN OLD;
END;
$_$ LANGUAGE plpgsql;


--
-- Trigger function to set indent and children on the new prefix.
--
CREATE OR REPLACE FUNCTION tf_ip_net_plan__indent_children__iu_before() RETURNS trigger AS $_$
DECLARE
	new_parent record;
BEGIN
	SELECT * INTO new_parent FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND iprange(prefix) >> iprange(NEW.prefix) ORDER BY prefix DESC LIMIT 1;

	IF TG_OP = 'UPDATE' THEN
		NEW.children := (SELECT COUNT(1) FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND iprange(prefix) << iprange(NEW.prefix) AND prefix != OLD.prefix AND indent = COALESCE(new_parent.indent+1, 1));
	ELSE
		NEW.children := (SELECT COUNT(1) FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND iprange(prefix) << iprange(NEW.prefix) AND indent = COALESCE(new_parent.indent+1, 0));
	END IF;

	RETURN NEW;
END;
$_$ LANGUAGE plpgsql;


--
-- Trigger function to update various data once a prefix has been UPDATEd.
--
CREATE OR REPLACE FUNCTION tf_ip_net_plan__prefix_iu_after() RETURNS trigger AS $_$
DECLARE
	old_parent RECORD;
	new_parent RECORD;
	old_parent_pool RECORD;
	new_parent_pool RECORD;
	child RECORD;
	i_max_pref_len integer;
	p RECORD;
	free_prefixes numeric(40);
BEGIN
	i_max_pref_len := 32;
	IF TG_OP IN ('INSERT', 'UPDATE') THEN
		IF family(NEW.prefix) = 6 THEN
			i_max_pref_len := 128;
		END IF;
	END IF;

	--
	-- get old and new parents
	--
	IF TG_OP = 'UPDATE' THEN
		-- Note how we have to explicitly filter out NEW.prefix for UPDATEs as
		-- the table has already been updated and we risk getting ourself as
		-- old_parent.
		SELECT * INTO old_parent
		FROM ip_net_plan
		WHERE vrf_id = OLD.vrf_id
			AND iprange(prefix) >> iprange(OLD.prefix)
			AND prefix != NEW.prefix
		ORDER BY prefix DESC LIMIT 1;
	ELSIF TG_OP = 'DELETE' THEN
		SELECT * INTO old_parent
		FROM ip_net_plan
		WHERE vrf_id = OLD.vrf_id
			AND iprange(prefix) >> iprange(OLD.prefix)
		ORDER BY prefix DESC LIMIT 1;
	END IF;

	-- contains the parent prefix
	IF TG_OP != 'DELETE' THEN
		SELECT * INTO new_parent
		FROM ip_net_plan
		WHERE vrf_id = NEW.vrf_id
			AND iprange(prefix) >> iprange(NEW.prefix)
		ORDER BY prefix DESC LIMIT 1;
	END IF;

	-- store old and new parents pool
	IF TG_OP IN ('DELETE', 'UPDATE') THEN
		SELECT * INTO old_parent_pool FROM ip_net_pool WHERE id = old_parent.pool_id;
	END IF;
	IF TG_OP IN ('INSERT', 'UPDATE') THEN
		SELECT * INTO new_parent_pool FROM ip_net_pool WHERE id = new_parent.pool_id;
	END IF;

	--
	---- indent ----------------------------------------------------------------
	--
	-- Trigger on: vrf_id, prefix
	--
	IF TG_OP = 'DELETE' THEN
		-- remove one indentation level where the old prefix used to be
		PERFORM calc_indent(OLD.vrf_id, OLD.prefix, -1);
	ELSIF TG_OP = 'INSERT' THEN
		-- add one indentation level to where the new prefix is
		PERFORM calc_indent(NEW.vrf_id, NEW.prefix, 1);
	ELSIF TG_OP = 'UPDATE' AND OLD.prefix != NEW.prefix THEN
		-- remove one indentation level where the old prefix used to be
		PERFORM calc_indent(OLD.vrf_id, OLD.prefix, -1);
		-- add one indentation level to where the new prefix is
		PERFORM calc_indent(NEW.vrf_id, NEW.prefix, 1);
	END IF;


	--
	---- children ----------------------------------------------------------------
	--
	-- Trigger on: vrf_id, prefix
	--
	-- This only sets the number of children prefix for the old or new parent
	-- prefix. The number of children for the prefix being modified is
	-- calculated in the before trigger.
	--
	-- NOTE: this is dependent upon indent already being correctly set
	-- NOTE: old and new parent needs to be set
	--
	IF TG_OP IN ('DELETE', 'UPDATE') THEN
		-- do we have a old parent? if not, this is a top level prefix and we
		-- have no parent to update children count for!
		IF old_parent.id IS NOT NULL THEN
			UPDATE ip_net_plan SET children =
					(SELECT COUNT(1)
					FROM ip_net_plan
					WHERE vrf_id = OLD.vrf_id
						AND iprange(prefix) << iprange(old_parent.prefix)
						AND indent = old_parent.indent+1)
				WHERE id = old_parent.id;
		END IF;
	END IF;

	IF TG_OP IN ('INSERT', 'UPDATE') THEN
		-- do we have a new parent? if not, this is a top level prefix and we
		-- have no parent to update children count for!
		IF new_parent.id IS NOT NULL THEN
			UPDATE ip_net_plan SET children =
					(SELECT COUNT(1)
					FROM ip_net_plan
					WHERE vrf_id = NEW.vrf_id
						AND iprange(prefix) << iprange(new_parent.prefix)
						AND indent = new_parent.indent+1)
				WHERE id = new_parent.id;
		END IF;
	END IF;



	--
	---- display_prefix update -------------------------------------------------
	-- update display_prefix of direct child prefixes which are hosts
	--
	-- Trigger: prefix
	--
	IF TG_OP = 'UPDATE' THEN
		-- display_prefix only differs from prefix on hosts and the only reason the
		-- display_prefix would change is if the covering assignment is changed
		IF NEW.type = 'assignment' AND OLD.prefix != NEW.prefix THEN
			UPDATE ip_net_plan SET display_prefix = set_masklen(prefix::inet, masklen(NEW.prefix)) WHERE vrf_id = NEW.vrf_id AND prefix << NEW.prefix;
		END IF;
	END IF;


	--
	---- Prefix statistics -----------------------------------------------------
	--
	-- Trigger on: vrf_id, prefix
	--

	-- update old and new parent
	IF TG_OP = 'DELETE' THEN
		-- do we have a old parent? if not, this is a top level prefix and we
		-- have no parent to update children count for!
		IF old_parent.id IS NOT NULL THEN
			-- update old parent's used and free addresses to account for the
			-- removal of 'this' prefix while increasing for previous indirect
			-- children that are now direct children of old parent
			UPDATE ip_net_plan SET
				used_addresses = old_parent.used_addresses - (OLD.total_addresses - CASE WHEN masklen(OLD.prefix) = i_max_pref_len THEN 0 ELSE OLD.used_addresses END),
				free_addresses = total_addresses - (old_parent.used_addresses - (OLD.total_addresses - CASE WHEN masklen(OLD.prefix) = i_max_pref_len THEN 0 ELSE OLD.used_addresses END))
				WHERE id = old_parent.id;
		END IF;
	ELSIF TG_OP = 'INSERT' THEN
		-- do we have a new parent? if not, this is a top level prefix and we
		-- have no parent to update children count for!
		IF new_parent.id IS NOT NULL THEN
			-- update new parent's used and free addresses to account for the
			-- addition of 'this' prefix while decreasing for previous direct
			-- children that are now covered by 'this'
			UPDATE ip_net_plan SET
				used_addresses = new_parent.used_addresses + (NEW.total_addresses - CASE WHEN masklen(NEW.prefix) = i_max_pref_len THEN 0 ELSE NEW.used_addresses END),
				free_addresses = total_addresses - (new_parent.used_addresses + (NEW.total_addresses - CASE WHEN masklen(NEW.prefix) = i_max_pref_len THEN 0 ELSE NEW.used_addresses END))
				WHERE id = new_parent.id;
		END IF;
	ELSIF TG_OP = 'UPDATE' THEN
		IF OLD.prefix != NEW.prefix THEN
			IF old_parent.id = new_parent.id THEN
				UPDATE ip_net_plan SET
					used_addresses = (new_parent.used_addresses - (OLD.total_addresses - CASE WHEN masklen(OLD.prefix) = i_max_pref_len THEN 0 ELSE OLD.used_addresses END)) + (NEW.total_addresses - CASE WHEN masklen(NEW.prefix) = i_max_pref_len THEN 0 ELSE NEW.used_addresses END),
					free_addresses = total_addresses - ((new_parent.used_addresses - (OLD.total_addresses - CASE WHEN masklen(OLD.prefix) = i_max_pref_len THEN 0 ELSE OLD.used_addresses END)) + (NEW.total_addresses - CASE WHEN masklen(NEW.prefix) = i_max_pref_len THEN 0 ELSE NEW.used_addresses END))
					WHERE id = new_parent.id;
			ELSE
				IF old_parent.id IS NOT NULL THEN
					-- update old parent's used and free addresses to account for the
					-- removal of 'this' prefix while increasing for previous indirect
					-- children that are now direct children of old parent
					UPDATE ip_net_plan SET
						used_addresses = old_parent.used_addresses - (OLD.total_addresses - CASE WHEN masklen(OLD.prefix) = i_max_pref_len THEN 0 ELSE OLD.used_addresses END),
						free_addresses = total_addresses - (old_parent.used_addresses - (OLD.total_addresses - CASE WHEN masklen(OLD.prefix) = i_max_pref_len THEN 0 ELSE OLD.used_addresses END))
						WHERE id = old_parent.id;
				END IF;
				IF new_parent.id IS NOT NULL THEN
					-- update new parent's used and free addresses to account for the
					-- addition of 'this' prefix while decreasing for previous direct
					-- children that are now covered by 'this'
					UPDATE ip_net_plan SET
						used_addresses = new_parent.used_addresses + (NEW.total_addresses - CASE WHEN masklen(NEW.prefix) = i_max_pref_len THEN 0 ELSE NEW.used_addresses END),
						free_addresses = total_addresses - (new_parent.used_addresses + (NEW.total_addresses - CASE WHEN masklen(NEW.prefix) = i_max_pref_len THEN 0 ELSE NEW.used_addresses END))
						WHERE id = new_parent.id;
				END IF;
			END IF;
		END IF;
	END IF;


	--
	---- VRF statistics --------------------------------------------------------
	--
	-- Trigger on: vrf_id, prefix, indent, used_addresses
	--

	-- update number of prefixes in VRF
	IF TG_OP = 'DELETE' THEN
		IF family(OLD.prefix) = 4 THEN
			UPDATE ip_net_vrf SET num_prefixes_v4 = num_prefixes_v4 - 1 WHERE id = OLD.vrf_id;
		ELSE
			UPDATE ip_net_vrf SET num_prefixes_v6 = num_prefixes_v6 - 1 WHERE id = OLD.vrf_id;
		END IF;
	ELSIF TG_OP = 'INSERT' THEN
		IF family(NEW.prefix) = 4 THEN
			UPDATE ip_net_vrf SET num_prefixes_v4 = num_prefixes_v4 + 1 WHERE id = NEW.vrf_id;
		ELSE
			UPDATE ip_net_vrf SET num_prefixes_v6 = num_prefixes_v6 + 1 WHERE id = NEW.vrf_id;
		END IF;
	END IF;
	-- update total / used / free addresses in VRF
	IF TG_OP = 'UPDATE' AND OLD.indent = 0 AND NEW.indent = 0 AND (OLD.prefix != NEW.prefix OR OLD.used_addresses != NEW.used_addresses) THEN
		-- we were and still are a top level prefix but used_addresses changed
		IF family(OLD.prefix) = 4 THEN
			UPDATE ip_net_vrf
			SET
				total_addresses_v4 = (total_addresses_v4 - OLD.total_addresses) + NEW.total_addresses,
				used_addresses_v4 = (used_addresses_v4 - OLD.used_addresses) + NEW.used_addresses,
				free_addresses_v4 = (free_addresses_v4 - OLD.free_addresses) + NEW.free_addresses
			WHERE id = OLD.vrf_id;
		ELSE
			UPDATE ip_net_vrf
			SET
				total_addresses_v6 = (total_addresses_v6 - OLD.total_addresses) + NEW.total_addresses,
				used_addresses_v6 = (used_addresses_v6 - OLD.used_addresses) + NEW.used_addresses,
				free_addresses_v6 = (free_addresses_v6 - OLD.free_addresses) + NEW.free_addresses
			WHERE id = OLD.vrf_id;
		END IF;
	ELSIF (TG_OP = 'DELETE' AND OLD.indent = 0) OR (TG_OP = 'UPDATE' AND NEW.indent > 0 AND OLD.indent = 0) THEN
		-- we were a top level prefix and became a NON top level
		IF family(OLD.prefix) = 4 THEN
			UPDATE ip_net_vrf
			SET
				total_addresses_v4 = total_addresses_v4 - OLD.total_addresses,
				used_addresses_v4 = used_addresses_v4 - OLD.used_addresses,
				free_addresses_v4 = free_addresses_v4 - OLD.free_addresses
			WHERE id = OLD.vrf_id;
		ELSE
			UPDATE ip_net_vrf
			SET
				total_addresses_v6 = total_addresses_v6 - OLD.total_addresses,
				used_addresses_v6 = used_addresses_v6 - OLD.used_addresses,
				free_addresses_v6 = free_addresses_v6 - OLD.free_addresses
			WHERE id = OLD.vrf_id;
		END IF;
	ELSIF (TG_OP = 'INSERT' AND NEW.indent = 0) OR (TG_OP = 'UPDATE' AND OLD.indent > 0 AND NEW.indent = 0) THEN
		-- we were a NON top level prefix and became a top level
		IF family(NEW.prefix) = 4 THEN
			UPDATE ip_net_vrf
			SET
				total_addresses_v4 = total_addresses_v4 + NEW.total_addresses,
				used_addresses_v4 = used_addresses_v4 + NEW.used_addresses,
				free_addresses_v4 = free_addresses_v4 + NEW.free_addresses
			WHERE id = NEW.vrf_id;
		ELSE
			UPDATE ip_net_vrf
			SET
				total_addresses_v6 = total_addresses_v6 + NEW.total_addresses,
				used_addresses_v6 = used_addresses_v6 + NEW.used_addresses,
				free_addresses_v6 = free_addresses_v6 + NEW.free_addresses
			WHERE id = NEW.vrf_id;
		END IF;
	END IF;

	--
	---- Pool statistics -------------------------------------------------------
	--
	-- Update pool statistics
	--

	-- we are a member prefix of a pool, update pool total
	IF TG_OP = 'DELETE' OR (TG_OP = 'UPDATE' AND (OLD.pool_id IS DISTINCT FROM NEW.pool_id OR OLD.prefix != NEW.prefix)) THEN
		free_prefixes := calc_pool_free_prefixes(OLD.pool_id, family(OLD.prefix));
		IF family(OLD.prefix) = 4 THEN
			UPDATE ip_net_pool
			SET member_prefixes_v4 = member_prefixes_v4 - 1,
				used_prefixes_v4 = used_prefixes_v4 - OLD.children,
				free_prefixes_v4 = free_prefixes,
				total_prefixes_v4 = (used_prefixes_v4 - OLD.children) + free_prefixes,
				total_addresses_v4 = total_addresses_v4 - OLD.total_addresses,
				free_addresses_v4 = free_addresses_v4 - OLD.free_addresses,
				used_addresses_v4 = used_addresses_v4 - OLD.used_addresses
			WHERE id = OLD.pool_id;
		ELSE
			UPDATE ip_net_pool
			SET member_prefixes_v6 = member_prefixes_v6 - 1,
				used_prefixes_v6 = used_prefixes_v6 - OLD.children,
				free_prefixes_v6 = free_prefixes,
				total_prefixes_v6 = (used_prefixes_v6 - OLD.children) + free_prefixes,
				total_addresses_v6 = total_addresses_v6 - OLD.total_addresses,
				free_addresses_v6 = free_addresses_v6 - OLD.free_addresses,
				used_addresses_v6 = used_addresses_v6 - OLD.used_addresses
			WHERE id = OLD.pool_id;
		END IF;
	END IF;
	IF TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND (OLD.pool_id IS DISTINCT FROM NEW.pool_id OR OLD.prefix != NEW.prefix)) THEN
		free_prefixes := calc_pool_free_prefixes(NEW.pool_id, family(NEW.prefix));
		IF family(NEW.prefix) = 4 THEN
			UPDATE ip_net_pool
			SET member_prefixes_v4 = member_prefixes_v4 + 1,
				used_prefixes_v4 = used_prefixes_v4 + NEW.children,
				free_prefixes_v4 = free_prefixes,
				total_prefixes_v4 = (used_prefixes_v4 + NEW.children) + free_prefixes,
				total_addresses_v4 = total_addresses_v4 + NEW.total_addresses,
				free_addresses_v4 = free_addresses_v4 + NEW.free_addresses,
				used_addresses_v4 = used_addresses_v4 + NEW.used_addresses
			WHERE id = NEW.pool_id;
		ELSE
			UPDATE ip_net_pool
			SET member_prefixes_v6 = member_prefixes_v6 + 1,
				used_prefixes_v6 = used_prefixes_v6 + NEW.children,
				free_prefixes_v6 = free_prefixes,
				total_prefixes_v6 = (used_prefixes_v6 + NEW.children) + free_prefixes,
				total_addresses_v6 = total_addresses_v6 + NEW.total_addresses,
				free_addresses_v6 = free_addresses_v6 + NEW.free_addresses,
				used_addresses_v6 = used_addresses_v6 + NEW.used_addresses
			WHERE id = NEW.pool_id;
		END IF;
	END IF;

	-- we are the child of a pool, ie our parent prefix is a member of the pool, update used / free
	IF (TG_OP = 'DELETE' OR (TG_OP = 'UPDATE' AND OLD.prefix != NEW.prefix)) THEN
		IF old_parent.pool_id IS NOT NULL THEN
			free_prefixes := calc_pool_free_prefixes(old_parent.pool_id, family(old_parent.prefix));
			IF family(OLD.prefix) = 4 THEN
				UPDATE ip_net_pool
				SET used_prefixes_v4 = used_prefixes_v4 - 1,
					free_prefixes_v4 = free_prefixes,
					total_prefixes_v4 = (used_prefixes_v4 - 1) + free_prefixes,
					free_addresses_v4 = free_addresses_v4 + OLD.total_addresses,
					used_addresses_v4 = used_addresses_v4 - OLD.total_addresses
				WHERE id = old_parent_pool.id;
			ELSE
				UPDATE ip_net_pool
				SET used_prefixes_v6 = used_prefixes_v6 - 1,
					free_prefixes_v6 = free_prefixes,
					total_prefixes_v6 = (used_prefixes_v6 - 1) + free_prefixes,
					free_addresses_v6 = free_addresses_v6 + OLD.total_addresses,
					used_addresses_v6 = used_addresses_v6 - OLD.total_addresses
				WHERE id = old_parent_pool.id;
			END IF;
		END IF;
	END IF;
	IF (TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND OLD.prefix != NEW.prefix)) THEN
		IF new_parent.pool_id IS NOT NULL THEN
			free_prefixes := calc_pool_free_prefixes(new_parent.pool_id, family(new_parent.prefix));
			IF family(NEW.prefix) = 4 THEN
				UPDATE ip_net_pool
				SET used_prefixes_v4 = used_prefixes_v4 + 1,
					free_prefixes_v4 = free_prefixes,
					total_prefixes_v4 = (used_prefixes_v4 + 1) + free_prefixes,
					free_addresses_v4 = free_addresses_v4 - NEW.total_addresses,
					used_addresses_v4 = used_addresses_v4 + NEW.total_addresses
				WHERE id = new_parent_pool.id;
			ELSE
				UPDATE ip_net_pool
				SET used_prefixes_v6 = used_prefixes_v6 + 1,
					free_prefixes_v6 = free_prefixes,
					total_prefixes_v6 = (used_prefixes_v6 + 1) + free_prefixes,
					free_addresses_v6 = free_addresses_v6 - NEW.total_addresses,
					used_addresses_v6 = used_addresses_v6 + NEW.total_addresses
				WHERE id = new_parent_pool.id;
			END IF;
		END IF;
	END IF;

	--
	---- Inherited Tags --------------------------------------------------------
	-- Update inherited tags
	--
	-- Trigger: prefix, tags, inherited_tags
	--
	IF TG_OP = 'DELETE' THEN
		-- parent is NULL if we are top level
		IF old_parent.id IS NULL THEN
			-- calc tags from parent of the deleted prefix to what is now the
			-- direct children of the parent prefix
			PERFORM calc_tags(OLD.vrf_id, OLD.prefix);
		ELSE
			PERFORM calc_tags(OLD.vrf_id, old_parent.prefix);
		END IF;

	ELSIF TG_OP = 'INSERT' THEN
		-- now push tags from the new prefix to its children
		PERFORM calc_tags(NEW.vrf_id, NEW.prefix);
		IF NEW.children > 0 AND (NEW.tags != '{}' OR NEW.inherited_tags != '{}') THEN
			PERFORM calc_tags(NEW.vrf_id, NEW.prefix);
		END IF;
	ELSIF TG_OP = 'UPDATE' THEN
		PERFORM calc_tags(OLD.vrf_id, OLD.prefix);
		PERFORM calc_tags(NEW.vrf_id, NEW.prefix);
	END IF;


	-- all is well, return
	RETURN NEW;
END;
$_$ LANGUAGE plpgsql;


--
-- Trigger function to update inherited tags.
--
CREATE OR REPLACE FUNCTION tf_ip_net_plan__tags__iu_before() RETURNS trigger AS $$
DECLARE
	old_parent record;
	new_parent record;
BEGIN
	IF TG_OP = 'DELETE' THEN
		-- NOOP
	ELSIF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
		-- figure out parent prefix
		SELECT * INTO new_parent FROM ip_net_plan WHERE vrf_id = NEW.vrf_id AND iprange(prefix) >> iprange(NEW.prefix) ORDER BY prefix DESC LIMIT 1;
	END IF;

	RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tf_ip_net_pool__iu_before() RETURNS trigger AS $_$
BEGIN
	IF TG_OP = 'INSERT' THEN
		NEW.free_prefixes_v4 := calc_pool_free_prefixes(NEW, 4);
		NEW.total_prefixes_v4 := NEW.used_prefixes_v4 + NEW.free_prefixes_v4;
		NEW.free_prefixes_v6 := calc_pool_free_prefixes(NEW, 6);
		NEW.total_prefixes_v6 := NEW.used_prefixes_v6 + NEW.free_prefixes_v6;
	ELSIF TG_OP = 'UPDATE' THEN
		IF OLD.ipv4_default_prefix_length IS DISTINCT FROM NEW.ipv4_default_prefix_length THEN
			NEW.free_prefixes_v4 := calc_pool_free_prefixes(NEW, 4);
			NEW.total_prefixes_v4 := NEW.used_prefixes_v4 + NEW.free_prefixes_v4;
		END IF;

		IF OLD.ipv6_default_prefix_length IS DISTINCT FROM NEW.ipv6_default_prefix_length THEN
			NEW.free_prefixes_v6 := calc_pool_free_prefixes(NEW, 6);
			NEW.total_prefixes_v6 := NEW.used_prefixes_v6 + NEW.free_prefixes_v6;
		END IF;
	END IF;

	RETURN NEW;
END;
$_$ LANGUAGE plpgsql;



--
-- Function used to remove all triggers before installation of new triggers
--
CREATE OR REPLACE FUNCTION clean_nipap_triggers() RETURNS bool AS $_$
DECLARE
	r record;
BEGIN
	FOR r IN (SELECT DISTINCT trigger_name FROM information_schema.triggers WHERE event_object_table = 'ip_net_vrf' AND trigger_schema NOT IN ('pg_catalog', 'information_schema')) LOOP
		EXECUTE 'DROP TRIGGER ' || r.trigger_name || ' ON ip_net_vrf';
	END LOOP;
	FOR r IN (SELECT DISTINCT trigger_name FROM information_schema.triggers WHERE event_object_table = 'ip_net_plan' AND trigger_schema NOT IN ('pg_catalog', 'information_schema')) LOOP
		EXECUTE 'DROP TRIGGER ' || r.trigger_name || ' ON ip_net_plan';
	END LOOP;
	FOR r IN (SELECT DISTINCT trigger_name FROM information_schema.triggers WHERE event_object_table = 'ip_net_pool' AND trigger_schema NOT IN ('pg_catalog', 'information_schema')) LOOP
		EXECUTE 'DROP TRIGGER ' || r.trigger_name || ' ON ip_net_pool';
	END LOOP;

	RETURN true;
END;
$_$ LANGUAGE plpgsql;

SELECT clean_nipap_triggers();

--
-- Triggers for sanity checking on ip_net_vrf table.
--
CREATE TRIGGER trigger_ip_net_vrf__iu_before
	BEFORE UPDATE OR INSERT
	ON ip_net_vrf
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_vrf_iu_before();

CREATE TRIGGER trigger_ip_net_vrf__d_before
	BEFORE DELETE
	ON ip_net_vrf
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_vrf_d_before();



--
-- Triggers for consistency checking and updating indent level on ip_net_plan
-- table.
--


-- sanity checking of INSERTs on ip_net_plan
CREATE TRIGGER trigger_ip_net_plan__i_before
	BEFORE INSERT
	ON ip_net_plan
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_plan__prefix_iu_before();

-- sanity checking of UPDATEs on ip_net_plan
CREATE TRIGGER trigger_ip_net_plan__vrf_prefix_type__u_before
	BEFORE UPDATE
	ON ip_net_plan
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_plan__prefix_iu_before();

-- actions to be performed after an UPDATE on ip_net_plan
-- sanity checks are performed in the before trigger, so this is only to
-- execute various changes that need to happen once a prefix has been updated
CREATE TRIGGER trigger_ip_net_plan__vrf_prefix_type__id_after
	AFTER INSERT OR DELETE
	ON ip_net_plan
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_plan__prefix_iu_after();

CREATE TRIGGER trigger_ip_net_plan__vrf_prefix_type__u_after
	AFTER UPDATE OF vrf_id, prefix, indent, type, pool_id, used_addresses, tags, inherited_tags
	ON ip_net_plan
	FOR EACH ROW
	WHEN (OLD.vrf_id != NEW.vrf_id
		OR OLD.prefix != NEW.prefix
		OR OLD.indent != NEW.indent
		OR OLD.type != NEW.type
		OR OLD.tags IS DISTINCT FROM NEW.tags
		OR OLD.inherited_tags IS DISTINCT FROM NEW.inherited_tags
		OR OLD.pool_id IS DISTINCT FROM NEW.pool_id
		OR OLD.used_addresses != NEW.used_addresses)
	EXECUTE PROCEDURE tf_ip_net_plan__prefix_iu_after();

-- check country code is correct
CREATE TRIGGER trigger_ip_net_plan__other__i_before
	BEFORE INSERT
	ON ip_net_plan
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_plan__other__iu_before();

CREATE TRIGGER trigger_ip_net_plan__other__u_before
	BEFORE UPDATE OF country
	ON ip_net_plan
	FOR EACH ROW
	WHEN (OLD.country != NEW.country)
	EXECUTE PROCEDURE tf_ip_net_plan__other__iu_before();


-- ip_net_plan - update indent and number of children
CREATE TRIGGER trigger_ip_net_plan__indent_children__i_before
	BEFORE INSERT
	ON ip_net_plan
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_plan__indent_children__iu_before();

CREATE TRIGGER trigger_ip_net_plan__indent_children__u_before
	BEFORE UPDATE OF prefix
	ON ip_net_plan
	FOR EACH ROW
	WHEN (OLD.prefix != NEW.prefix)
	EXECUTE PROCEDURE tf_ip_net_plan__indent_children__iu_before();

CREATE TRIGGER trigger_ip_net_plan_prefix__d_before
	BEFORE DELETE
	ON ip_net_plan
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_prefix_d_before();


-- ip_net_pool
CREATE TRIGGER trigger_ip_net_pool__i_before
	BEFORE INSERT
	ON ip_net_pool
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_pool__iu_before();

CREATE TRIGGER trigger_ip_net_pool__u_before
	BEFORE UPDATE OF ipv4_default_prefix_length, ipv6_default_prefix_length
	ON ip_net_pool
	FOR EACH ROW
	WHEN (OLD.ipv4_default_prefix_length IS DISTINCT FROM NEW.ipv4_default_prefix_length
		OR OLD.ipv6_default_prefix_length IS DISTINCT FROM NEW.ipv6_default_prefix_length)
	EXECUTE PROCEDURE tf_ip_net_pool__iu_before();
"""

upgrade = [
"""
--
-- Upgrade from NIPAP database schema version 1 to 2
--

-- rename trigger function
DROP TRIGGER trigger_ip_net_plan_prefix__iu_after ON ip_net_plan;
CREATE TRIGGER trigger_ip_net_plan_prefix__iu_after
	AFTER DELETE OR INSERT OR UPDATE
	ON ip_net_plan
	FOR EACH ROW
	EXECUTE PROCEDURE tf_ip_net_prefix_after();
DROP FUNCTION tf_ip_net_prefix_family_after();

-- add children
ALTER TABLE ip_net_plan ADD COLUMN children integer;
COMMENT ON COLUMN ip_net_plan.children IS 'Number of direct sub-prefixes';

-- vlan support
ALTER TABLE ip_net_plan ADD COLUMN vlan integer;
COMMENT ON COLUMN ip_net_plan.vlan IS 'VLAN ID';

-- add tags
ALTER TABLE ip_net_plan ADD COLUMN tags text[] DEFAULT '{}';
ALTER TABLE ip_net_plan ADD COLUMN inherited_tags text[] DEFAULT '{}';
COMMENT ON COLUMN ip_net_plan.tags IS 'Tags associated with the prefix';
COMMENT ON COLUMN ip_net_plan.inherited_tags IS 'Tags inherited from parent (and grand-parent) prefixes';

-- timestamp columns
ALTER TABLE ip_net_plan ADD COLUMN added timestamp with time zone DEFAULT NOW();
ALTER TABLE ip_net_plan ADD COLUMN last_modified timestamp with time zone DEFAULT NOW();
COMMENT ON COLUMN ip_net_plan.added IS 'The date and time when the prefix was added';
COMMENT ON COLUMN ip_net_plan.last_modified IS 'The date and time when the prefix was last modified';
-- set added column to timestamp of first audit entry
UPDATE ip_net_plan inp SET added = (SELECT timestamp FROM ip_net_log inl WHERE inl.prefix_id = inp.id ORDER BY inl.timestamp DESC LIMIT 1);

-- update database schema version
COMMENT ON DATABASE nipap IS 'NIPAP database - schema version: 2';
""",
"""
--
-- Upgrade from NIPAP database schema version 2 to 3
--

-- add customer_id 
ALTER TABLE ip_net_plan ADD COLUMN customer_id text;
COMMENT ON COLUMN ip_net_plan.customer_id IS 'Customer Identifier';

-- update database schema version
COMMENT ON DATABASE nipap IS 'NIPAP database - schema version: 3';

CREATE OR REPLACE FUNCTION update_children() RETURNS boolean AS $_$
DECLARE
	r record;
	num_children integer;
BEGIN
	FOR r IN (SELECT * FROM ip_net_plan) LOOP
		num_children := (SELECT COALESCE((
			SELECT COUNT(1)
			FROM ip_net_plan
			WHERE vrf_id = r.vrf_id
				AND prefix << r.prefix
				AND indent = r.indent+1), 0));
		UPDATE ip_net_plan SET children = num_children WHERE id = r.id;
	END LOOP;

	RETURN true;
END;
$_$ LANGUAGE plpgsql;

SELECT update_children();
DROP FUNCTION update_children();
""",
"""
--
-- Upgrade from NIPAP database schema version 3 to 4
--

-- add statistics to vrf table
ALTER TABLE ip_net_vrf ADD COLUMN num_prefixes_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_vrf ADD COLUMN num_prefixes_v6 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_vrf ADD COLUMN total_addresses_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_vrf ADD COLUMN total_addresses_v6 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_vrf ADD COLUMN used_addresses_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_vrf ADD COLUMN used_addresses_v6 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_vrf ADD COLUMN free_addresses_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_vrf ADD COLUMN free_addresses_v6 numeric(40) DEFAULT 0;

COMMENT ON COLUMN ip_net_vrf.num_prefixes_v4 IS 'Number of IPv4 prefixes in this VRF';
COMMENT ON COLUMN ip_net_vrf.num_prefixes_v6 IS 'Number of IPv6 prefixes in this VRF';
COMMENT ON COLUMN ip_net_vrf.total_addresses_v4 IS 'Total number of IPv4 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.total_addresses_v6 IS 'Total number of IPv6 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.used_addresses_v4 IS 'Number of used IPv4 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.used_addresses_v6 IS 'Number of used IPv6 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.free_addresses_v4 IS 'Number of free IPv4 addresses in this VRF';
COMMENT ON COLUMN ip_net_vrf.free_addresses_v6 IS 'Number of free IPv6 addresses in this VRF';

-- add statistics to prefix table
ALTER TABLE ip_net_plan ADD COLUMN total_addresses numeric(40) DEFAULT 0;
ALTER TABLE ip_net_plan ADD COLUMN used_addresses numeric(40) DEFAULT 0;
ALTER TABLE ip_net_plan ADD COLUMN free_addresses numeric(40) DEFAULT 0;
COMMENT ON COLUMN ip_net_plan.total_addresses IS 'Total number of addresses in this prefix';
COMMENT ON COLUMN ip_net_plan.used_addresses IS 'Number of used addresses in this prefix';
COMMENT ON COLUMN ip_net_plan.free_addresses IS 'Number of free addresses in this prefix';

-- add statistics to pool table
ALTER TABLE ip_net_pool ADD COLUMN member_prefixes_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN member_prefixes_v6 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN used_prefixes_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN used_prefixes_v6 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN total_addresses_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN total_addresses_v6 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN used_addresses_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN used_addresses_v6 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN free_addresses_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN free_addresses_v6 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN free_prefixes_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN free_prefixes_v6 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN total_prefixes_v4 numeric(40) DEFAULT 0;
ALTER TABLE ip_net_pool ADD COLUMN total_prefixes_v6 numeric(40) DEFAULT 0;

COMMENT ON COLUMN ip_net_pool.member_prefixes_v4 IS 'Number of IPv4 prefixes that are members of this pool';
COMMENT ON COLUMN ip_net_pool.member_prefixes_v6 IS 'Number of IPv6 prefixes that are members of this pool';
COMMENT ON COLUMN ip_net_pool.used_prefixes_v4 IS 'Number of IPv4 prefixes allocated from this pool';
COMMENT ON COLUMN ip_net_pool.used_prefixes_v6 IS 'Number of IPv6 prefixes allocated from this pool';
COMMENT ON COLUMN ip_net_pool.total_addresses_v4 IS 'Total number of IPv4 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.total_addresses_v6 IS 'Total number of IPv6 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.used_addresses_v4 IS 'Number of used IPv4 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.used_addresses_v6 IS 'Number of used IPv6 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.free_addresses_v4 IS 'Number of free IPv4 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.free_addresses_v6 IS 'Number of free IPv6 addresses in this pool';
COMMENT ON COLUMN ip_net_pool.free_prefixes_v4 IS 'Number of potentially free IPv4 prefixes of the default assignment size';
COMMENT ON COLUMN ip_net_pool.free_prefixes_v6 IS 'Number of potentially free IPv6 prefixes of the default assignment size';
COMMENT ON COLUMN ip_net_pool.total_prefixes_v4 IS 'Potentially the total number of IPv4 child prefixes in pool. This is based on current number of childs and potential childs of the default assignment size, which is why it can vary.';
COMMENT ON COLUMN ip_net_pool.total_prefixes_v6 IS 'Potentially the total number of IPv6 child prefixes in pool. This is based on current number of childs and potential childs of the default assignment size, which is why it can vary.';

--
-- set stats for the first time
--
-- prefix stats
UPDATE ip_net_plan SET total_addresses = power(2::numeric, (CASE WHEN family(prefix) = 4 THEN 32 ELSE 128 END) - masklen(prefix));
UPDATE ip_net_plan AS inp SET used_addresses = COALESCE((SELECT SUM(total_addresses) FROM ip_net_plan AS inp2 WHERE inp2.prefix << inp.prefix AND inp2.indent = inp.indent + 1), 0);
UPDATE ip_net_plan SET free_addresses = total_addresses - used_addresses;
-- vrf stats
UPDATE ip_net_vrf SET num_prefixes_v4 = (SELECT COUNT(1) FROM ip_net_plan WHERE vrf_id = ip_net_vrf.id AND family(prefix) = 4);
UPDATE ip_net_vrf SET num_prefixes_v6 = (SELECT COUNT(1) FROM ip_net_plan WHERE vrf_id = ip_net_vrf.id AND family(prefix) = 6);
UPDATE ip_net_vrf SET total_addresses_v4 = COALESCE((SELECT SUM(total_addresses) FROM ip_net_plan WHERE vrf_id = ip_net_vrf.id AND indent = 0 AND family(prefix) = 4), 0);
UPDATE ip_net_vrf SET total_addresses_v6 = COALESCE((SELECT SUM(total_addresses) FROM ip_net_plan WHERE vrf_id = ip_net_vrf.id AND indent = 0 AND family(prefix) = 6), 0);
UPDATE ip_net_vrf SET used_addresses_v4 = COALESCE((SELECT SUM(used_addresses) FROM ip_net_plan WHERE vrf_id = ip_net_vrf.id AND indent = 0 AND family(prefix) = 4), 0);
UPDATE ip_net_vrf SET used_addresses_v6 = COALESCE((SELECT SUM(used_addresses) FROM ip_net_plan WHERE vrf_id = ip_net_vrf.id AND indent = 0 AND family(prefix) = 6), 0);
UPDATE ip_net_vrf SET free_addresses_v4 = COALESCE((SELECT SUM(free_addresses) FROM ip_net_plan WHERE vrf_id = ip_net_vrf.id AND indent = 0 AND family(prefix) = 4), 0);
UPDATE ip_net_vrf SET free_addresses_v6 = COALESCE((SELECT SUM(free_addresses) FROM ip_net_plan WHERE vrf_id = ip_net_vrf.id AND indent = 0 AND family(prefix) = 6), 0);
-- pool stats
UPDATE ip_net_pool SET member_prefixes_v4 = (SELECT COUNT(1) FROM ip_net_plan WHERE pool_id = ip_net_pool.id AND family(prefix) = 4);
UPDATE ip_net_pool SET member_prefixes_v6 = (SELECT COUNT(1) FROM ip_net_plan WHERE pool_id = ip_net_pool.id AND family(prefix) = 6);
UPDATE ip_net_pool SET used_prefixes_v4 = (SELECT COUNT(1) from ip_net_plan AS inp JOIN ip_net_plan AS inp2 ON (inp2.prefix << inp.prefix AND inp2.indent = inp.indent+1 AND family(inp.prefix) = 4) WHERE inp.pool_id = ip_net_pool.id);
UPDATE ip_net_pool SET used_prefixes_v6 = (SELECT COUNT(1) from ip_net_plan AS inp JOIN ip_net_plan AS inp2 ON (inp2.prefix << inp.prefix AND inp2.indent = inp.indent+1 AND family(inp.prefix) = 6) WHERE inp.pool_id = ip_net_pool.id);
UPDATE ip_net_pool SET total_addresses_v4 = COALESCE((SELECT SUM(total_addresses) FROM ip_net_plan WHERE pool_id = ip_net_pool.id AND family(prefix) = 4), 0);
UPDATE ip_net_pool SET total_addresses_v6 = COALESCE((SELECT SUM(total_addresses) FROM ip_net_plan WHERE pool_id = ip_net_pool.id AND family(prefix) = 6), 0);
UPDATE ip_net_pool SET used_addresses_v4 = COALESCE((SELECT SUM(used_addresses) FROM ip_net_plan WHERE pool_id = ip_net_pool.id AND family(prefix) = 4), 0);
UPDATE ip_net_pool SET used_addresses_v6 = COALESCE((SELECT SUM(used_addresses) FROM ip_net_plan WHERE pool_id = ip_net_pool.id AND family(prefix) = 6), 0);
UPDATE ip_net_pool SET free_addresses_v4 = COALESCE((SELECT SUM(free_addresses) FROM ip_net_plan WHERE pool_id = ip_net_pool.id AND family(prefix) = 4), 0);
UPDATE ip_net_pool SET free_addresses_v6 = COALESCE((SELECT SUM(free_addresses) FROM ip_net_plan WHERE pool_id = ip_net_pool.id AND family(prefix) = 6), 0);
-- TODO: implement this!
--UPDATE ip_net_pool SET free_prefixes_v4 = 
--UPDATE ip_net_pool SET free_prefixes_v6 = 
--UPDATE ip_net_pool SET total_prefixes_v4 = 
--UPDATE ip_net_pool SET total_prefixes_v6 = 

-- add pool_id index
CREATE INDEX ip_net_plan__pool_id__index ON ip_net_plan (pool_id);

-- update database schema version
COMMENT ON DATABASE nipap IS 'NIPAP database - schema version: 4';
""",
"""
--
-- Upgrade from NIPAP database schema version 4 to 5
--

--
-- Recalculate all statistics from scratch
--
CREATE OR REPLACE FUNCTION recalculate_statistics() RETURNS bool AS $_$
DECLARE
	i int;
BEGIN
	UPDATE ip_net_plan SET total_addresses = power(2::numeric, (CASE WHEN family(prefix) = 4 THEN 32 ELSE 128 END) - masklen(prefix)) WHERE family(prefix) = 4;
	UPDATE ip_net_plan SET total_addresses = power(2::numeric, (CASE WHEN family(prefix) = 4 THEN 32 ELSE 128 END) - masklen(prefix)) WHERE family(prefix) = 6;

	FOR i IN (SELECT generate_series(31, 0, -1)) LOOP
		--RAISE WARNING 'Calculating statistics for IPv4/%', i;
		UPDATE ip_net_plan AS inp SET used_addresses = COALESCE((SELECT SUM(total_addresses) FROM ip_net_plan AS inp2 WHERE iprange(inp2.prefix) << iprange(inp.prefix) AND inp2.indent = inp.indent + 1), CASE WHEN (family(prefix) = 4 AND masklen(prefix) = 32) OR (family(prefix) = 6 AND masklen(prefix) = 128) THEN 1 ELSE 0 END) WHERE family(prefix) = 4 AND masklen(prefix) = i;
		UPDATE ip_net_plan SET free_addresses = total_addresses - used_addresses WHERE family(prefix) = 4 AND masklen(prefix) = i;
	END LOOP;

	FOR i IN (SELECT generate_series(127, 0, -1)) LOOP
		--RAISE WARNING 'Calculating statistics for IPv4/%', i;
		UPDATE ip_net_plan AS inp SET used_addresses = COALESCE((SELECT SUM(total_addresses) FROM ip_net_plan AS inp2 WHERE iprange(inp2.prefix) << iprange(inp.prefix) AND inp2.indent = inp.indent + 1), CASE WHEN (family(prefix) = 4 AND masklen(prefix) = 32) OR (family(prefix) = 6 AND masklen(prefix) = 128) THEN 1 ELSE 0 END) WHERE family(prefix) = 6 AND masklen(prefix) = i;
		UPDATE ip_net_plan SET free_addresses = total_addresses - used_addresses WHERE family(prefix) = 4 AND masklen(prefix) = i;
	END LOOP;

	RETURN true;
END;
$_$ LANGUAGE plpgsql;

-- hstore extension is required for AVPs
CREATE EXTENSION IF NOT EXISTS hstore;

-- change default values for pool prefix statistics columns
ALTER TABLE ip_net_pool ALTER COLUMN free_prefixes_v4 SET DEFAULT NULL;
ALTER TABLE ip_net_pool ALTER COLUMN free_prefixes_v6 SET DEFAULT NULL;
ALTER TABLE ip_net_pool ALTER COLUMN total_prefixes_v4 SET DEFAULT NULL;
ALTER TABLE ip_net_pool ALTER COLUMN total_prefixes_v6 SET DEFAULT NULL;

-- update improved pool statistics
UPDATE ip_net_pool SET free_prefixes_v4 = calc_pool_free_prefixes(id, 4);
UPDATE ip_net_pool SET free_prefixes_v6 = calc_pool_free_prefixes(id, 6);
UPDATE ip_net_pool SET total_prefixes_v4 = used_prefixes_v4 + free_prefixes_v4, total_prefixes_v6 = used_prefixes_v6 + free_prefixes_v6;

-- add VRF tags
ALTER TABLE ip_net_vrf ADD COLUMN tags text[] DEFAULT '{}';
COMMENT ON COLUMN ip_net_vrf.tags IS 'Tags associated with the VRF';

-- add pool tags
ALTER TABLE ip_net_pool ADD COLUMN tags text[] DEFAULT '{}';
COMMENT ON COLUMN ip_net_pool.tags IS 'Tags associated with the pool';

-- prefix stats
SELECT recalculate_statistics();

-- add status field
CREATE TYPE ip_net_plan_status AS ENUM ('assigned', 'reserved', 'quarantine');
ALTER TABLE ip_net_plan ADD COLUMN status ip_net_plan_status NOT NULL DEFAULT 'assigned';

-- add AVP column
ALTER TABLE ip_net_vrf ADD COLUMN avps hstore NOT NULL DEFAULT '';
ALTER TABLE ip_net_plan ADD COLUMN avps hstore NOT NULL DEFAULT '';
ALTER TABLE ip_net_pool ADD COLUMN avps hstore NOT NULL DEFAULT '';

-- add expires field
ALTER TABLE ip_net_plan ADD COLUMN expires timestamp with time zone DEFAULT 'infinity';

-- update database schema version
COMMENT ON DATABASE nipap IS 'NIPAP database - schema version: 5';
""",
"""
--
-- Upgrade from NIPAP database schema version 5 to 6
--

CREATE EXTENSION IF NOT EXISTS citext;

ALTER TABLE ip_net_vrf ALTER COLUMN name SET NOT NULL;

DROP INDEX ip_net_vrf__name__index;
CREATE UNIQUE INDEX ip_net_vrf__name__index ON ip_net_vrf (lower(name)) WHERE name IS NOT NULL;

ALTER TABLE ip_net_pool DROP CONSTRAINT ip_net_pool_name_key;
CREATE UNIQUE INDEX ip_net_pool__name__index ON ip_net_pool (lower(name));

-- update database schema version
COMMENT ON DATABASE nipap IS 'NIPAP database - schema version: 6';
"""
]
