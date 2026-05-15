-- Sector classification fixes — Category A from the 2026-05-15 audit.
-- Source of truth: Screener.in BSE sectoral breadcrumb (sector_audit_screener.csv)
--
-- Excluded from this batch (judgment calls — discuss separately):
--   RELIANCE.NS, ADANIENT.NS  — kept as CONGLOMERATE pending decision
--   WABAG.NS                  — water utility, no UTILITIES bucket; keep INFRASTRUCTURE

BEGIN;

-- ── Hard errors: wrong name AND wrong sector ────────────────────────
-- JWL.NS is Jupiter Wagons (railway), NOT Jet Airways (delisted).
UPDATE indian_stock_sectors
   SET company_name = 'JUPITER WAGONS LIMITED',
       sector       = 'INDUSTRIAL',
       updated_at   = NOW()
 WHERE ticker = 'JWL.NS';

-- INDOCO.NS is Indoco Remedies (pharma), NOT Indo Count Industries (textiles).
UPDATE indian_stock_sectors
   SET company_name = 'INDOCO REMEDIES LIMITED',
       sector       = 'HEALTHCARE',
       updated_at   = NOW()
 WHERE ticker = 'INDOCO.NS';

-- ── Sector-only reclassifications ───────────────────────────────────
UPDATE indian_stock_sectors
   SET sector = CASE ticker
     -- Energy reclassifications
     WHEN 'AEGISLOG.NS'   THEN 'ENERGY'           -- LPG/gas terminals
     WHEN 'COALINDIA.NS'  THEN 'ENERGY'           -- coal is energy, not metals

     -- Healthcare
     WHEN 'APLLTD.NS'     THEN 'HEALTHCARE'       -- APL Limited is a pharma company

     -- Metals
     WHEN 'NSLNISP.NS'    THEN 'METALS'           -- iron & steel producer

     -- Media
     WHEN 'HATHWAY.NS'    THEN 'MEDIA'            -- cable TV operator
     WHEN 'PVRINOX.NS'    THEN 'MEDIA'            -- film exhibition

     -- Textiles
     WHEN 'PAGEIND.NS'    THEN 'TEXTILES'         -- Jockey innerwear

     -- Consumer services / retail
     WHEN 'MANYAVAR.NS'   THEN 'CONSUMER SERVICES' -- ethnic apparel retail

     -- FMCG (stationery)
     WHEN 'DOMS.NS'       THEN 'FMCG'

     -- Financial services (pure holding cos)
     WHEN 'BAJAJHLDNG.NS' THEN 'FINANCIAL SERVICES'
     WHEN 'TATAINVEST.NS' THEN 'FINANCIAL SERVICES'

     -- Realty (post-demerger)
     WHEN 'RAYMOND.NS'    THEN 'REALTY'

     -- Telecom services
     WHEN 'ROUTE.NS'      THEN 'TELECOM'          -- messaging-as-a-service for telcos
     WHEN 'RAILTEL.NS'    THEN 'TELECOM'          -- telecom infrastructure

     -- Consumer durables (plywood/laminates)
     WHEN 'CENTURYPLY.NS' THEN 'CONSUMER DURABLES'
     WHEN 'GREENPANEL.NS' THEN 'CONSUMER DURABLES'

     -- Infrastructure (power transmission EPC, was wrongly tagged IT)
     WHEN 'TECHNOE.NS'    THEN 'INFRASTRUCTURE'

     ELSE sector
   END,
   updated_at = NOW()
 WHERE ticker IN (
     'AEGISLOG.NS','COALINDIA.NS','APLLTD.NS','NSLNISP.NS',
     'HATHWAY.NS','PVRINOX.NS','PAGEIND.NS','MANYAVAR.NS','DOMS.NS',
     'BAJAJHLDNG.NS','TATAINVEST.NS','RAYMOND.NS',
     'ROUTE.NS','RAILTEL.NS','CENTURYPLY.NS','GREENPANEL.NS','TECHNOE.NS'
   );

COMMIT;

-- Verify
SELECT ticker, company_name, sector
  FROM indian_stock_sectors
 WHERE ticker IN (
   'JWL.NS','INDOCO.NS','AEGISLOG.NS','COALINDIA.NS','APLLTD.NS','NSLNISP.NS',
   'HATHWAY.NS','PVRINOX.NS','PAGEIND.NS','MANYAVAR.NS','DOMS.NS',
   'BAJAJHLDNG.NS','TATAINVEST.NS','RAYMOND.NS',
   'ROUTE.NS','RAILTEL.NS','CENTURYPLY.NS','GREENPANEL.NS','TECHNOE.NS'
 )
 ORDER BY ticker;
