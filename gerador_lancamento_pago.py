#!/usr/bin/env python3
"""Gerador Dashboard Lançamento Pago v4"""

import pandas as pd, json, re, hashlib, requests
from datetime import date
from pathlib import Path

# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════
SHEET_ID         = "1kKFBRS1iEMKVgjBOxn-ndTgK5MMZdfd8pqrI3aa5EvQ"
TEMPLATE_FILE    = "dashboard_lancamento_pago.html"
OUTPUT_FILE      = "index.html"
NOME_CLIENTE     = "Rafa Rovani"
LOGO_LETRA       = "R"
COR_ACENTO       = "#C1994B"
LANCAMENTO_COD   = "QUEBRA02"      # filtra campanhas pelo código; "" = ver tudo
USAR_PESQUISA    = True            # False = oculta aba Pesquisa no menu e dashboard
USAR_ORIGEM      = False           # False = oculta "Vendas por Origem · Last Click" (reativar quando as UTMs da Eduzz estiverem confiáveis)
PRODUTOS_EDUZZ   = ["ALL"]         # "ALL" ou [] = todos | ["A QUEBRA"] = só esse | ["Produto A","Produto B"] = ambos
# Classificação Pago vs Orgânico via UTM (Eduzz manda utm_source/medium/campaign no webhook)
# Venda = PAGO se qualquer UTM bater com o padrão abaixo (regex, case-insensitive). Senão = Orgânico.
UTM_PAGO_REGEX   = "SOBE"          # padrão das UTMs de tráfego pago (ex: SOBE-QUEBRA02-...)

CPA_BOM          = 20
CPA_MEDIO        = 40
ROAS_BOM         = 1.0
ROAS_MEDIO       = 0.6

# Metas do funil — define cores (verde/amarelo/vermelho) nas taxas
# Cada métrica: [valor_bom, valor_medio] — acima do bom = verde, entre = amarelo, abaixo = vermelho
CTR_BOM          = 1.0    # CTR ≥ 1.2% → verde | 0.8-1.2% → amarelo | <0.8% → vermelho
CTR_MEDIO        = 0.8
CR_BOM           = 70.0   # Connect Rate ≥ 75% → verde | 63-75% → amarelo | <63% → vermelho
CR_MEDIO         = 63.0
TX_IC_BOM        = 20.0   # Tx Init Checkout ≥ 20% → verde | 15-20% → amarelo | <15% → vermelho
TX_IC_MEDIO      = 15.0
TX_CK_BOM        = 32.0   # Taxa Checkout ≥ 32% → verde | 23-32% → amarelo | <23% → vermelho
TX_CK_MEDIO      = 23.0
TX_CONV_BOM      = 8.0    # Taxa Conversão LP ≥ 8% → verde | 6-8% → amarelo | <5% → vermelho
TX_CONV_MEDIO    = 6.0

CPM_BOM          = 5.0    # CPM ≤ 5 → verde | 5-10 → amarelo | >10 → vermelho (menor = melhor)
CPM_MEDIO        = 10.0

# ══════════════════════════════════════════════════════
def sheet_url(t): return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet={t}"
URL_META = sheet_url("meta-ads")
URL_HOT  = sheet_url("hotmart")
URL_PES  = sheet_url("Pesquisa")
URL_GA   = sheet_url("breakdown-gender-age")
URL_PT   = sheet_url("breakdown-platform")

def to_num(s):
    """Converte série para numérico — detecta formato BR (1.234,56) ou US (1234.56)"""
    if pd.api.types.is_numeric_dtype(s):
        return s.fillna(0)
    clean = s.astype(str).str.strip().str.replace("R$","",regex=False).str.strip()
    # Formato BR: tem vírgula como decimal (ex: "29,9" ou "1.234,56")
    if clean.str.contains(r"\d,\d", regex=True).any():
        clean = clean.str.replace(".","",regex=False).str.replace(",",".",regex=False)
    # Formato US ou sem separador: usar direto (não remover pontos)
    return pd.to_numeric(clean, errors="coerce").fillna(0)
def safe(v):
    if v is None or (isinstance(v,float) and pd.isna(v)): return None
    return round(float(v),2) if float(v)!=0 else None
def download_thumb(url, d):
    if not url or str(url)=="nan": return ""
    try:
        ext=".png" if ".png" in url.lower() else ".jpg"
        fname=hashlib.md5(url.encode()).hexdigest()[:16]+ext
        fp=d/fname
        if not fp.exists():
            r=requests.get(url,timeout=10,headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code==200: fp.write_bytes(r.content)
            else: return ""
        return "imgs/"+fname
    except: return ""

# ══ EDUZZ (carrega primeiro para ter ticket_medio) ══
def parse_eduzz_dates(s):
    """Datas do webhook Eduzz: YYYYMMDD (trans_paiddate). Aceita também dd/mm/yyyy e ISO."""
    s = s.astype(str).str.strip().str.replace(r"\.0$","",regex=True).str.split(" ").str[0]
    out = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
    m = out.isna()
    if m.any(): out[m] = pd.to_datetime(s[m], format="%d/%m/%Y", errors="coerce")
    m = out.isna()
    if m.any(): out[m] = pd.to_datetime(s[m], errors="coerce")
    return out

def load_hotmart():
    """Lê a aba 'hotmart' (dados Eduzz via webhook) e devolve df compatível com hotmart_process."""
    print("  Lendo eduzz (aba hotmart)...")
    df = pd.read_csv(URL_HOT)
    df.columns = [str(c).strip() for c in df.columns]

    # ── Status: só vendas pagas (trans_status=3 / event invoice_paid / texto "paga") ──
    st  = df.get("trans_status", pd.Series([""]*len(df))).astype(str).str.strip().str.replace(r"\.0$","",regex=True).str.lower()
    ev  = df.get("event_name",   pd.Series([""]*len(df))).astype(str).str.lower()
    pago_mask = st.isin(["3","paid","paga"]) | ev.str.contains("invoice_paid", na=False)
    df = df[pago_mask].copy()

    # ── Dedup: webhook manda 1 linha por mudança de status → manter 1 por fatura ──
    if "trans_cod" in df.columns:
        antes = len(df)
        df = df.drop_duplicates(subset=["trans_cod"], keep="last")
        if antes != len(df): print(f"     dedup: {antes-len(df)} linha(s) duplicada(s) removida(s)")

    # ── Data: paga → trans_paiddate; fallback trans_createdate ──
    df["date"] = parse_eduzz_dates(df["trans_paiddate"]) if "trans_paiddate" in df.columns else pd.NaT
    if "trans_createdate" in df.columns:
        m = df["date"].isna()
        if m.any(): df.loc[m,"date"] = parse_eduzz_dates(df.loc[m,"trans_createdate"])
    df = df.dropna(subset=["date"])
    df["date"] = df["date"].dt.normalize()

    # ── Valor ──
    df["valor"] = to_num(df["trans_value"])

    # ── Produto (compat com hotmart_process) ──
    df["Produto"] = df.get("product_name", pd.Series(["Produto"]*len(df))).astype(str)
    if PRODUTOS_EDUZZ and PRODUTOS_EDUZZ != ["ALL"]:
        df = df[df["Produto"].str.contains("|".join(PRODUTOS_EDUZZ), na=False, case=False)]

    # ── Pago vs Orgânico via UTM ──
    utms = pd.Series([""]*len(df), index=df.index)
    for c in ["utm_source","utm_medium","utm_campaign","tracker_utm_source","tracker_utm_medium","tracker_utm_campaign"]:
        if c in df.columns:
            utms = utms + " " + df[c].fillna("").astype(str)
    df["Organico ou Pago"] = utms.str.contains(UTM_PAGO_REGEX, case=False, regex=True).map({True:"Pago",False:"Orgânico"})

    print(f"     {len(df)} vendas pagas | R$ {df['valor'].sum():,.2f} | Pago: {(df['Organico ou Pago']=='Pago').sum()} / Orgânico: {(df['Organico ou Pago']=='Orgânico').sum()}")
    return df

def hotmart_process(df):
    total=df["valor"].sum(); qtd=len(df)
    ticket=round(float(total/qtd),2) if qtd>0 else 0
    orig_col=next((c for c in df.columns if "organico" in c.lower() or "orgânico" in c.lower() or "pago" in c.lower()), "Organico ou Pago")
    pago=df[df[orig_col].str.contains("Pago",na=False,case=False)]
    org =df[df[orig_col].str.contains("Orgân",na=False,case=False)]
    # Vendas por produto (pago vs orgânico)
    por_produto=[]
    prod_col2=next((c for c in df.columns if "produto" in c.lower()), "Produto")
    orig_col2=next((c for c in df.columns if "organico" in c.lower() or "orgânico" in c.lower() or "pago" in c.lower()), "Organico ou Pago")
    for prod, gdf in df.groupby(prod_col2):
        p_pago=gdf[gdf[orig_col2].str.contains("Pago",na=False,case=False)]
        p_org =gdf[gdf[orig_col2].str.contains("Orgân",na=False,case=False)]
        por_produto.append({
            "produto": str(prod),
            "total_qtd": int(len(gdf)),
            "total_val": round(float(gdf["valor"].sum()),2),
            "pago_qtd":  int(len(p_pago)),
            "pago_val":  round(float(p_pago["valor"].sum()),2),
            "org_qtd":   int(len(p_org)),
            "org_val":   round(float(p_org["valor"].sum()),2),
        })
    por_produto.sort(key=lambda x: x["total_val"], reverse=True)

    kpis={"total":round(float(total),2),"qtd":int(qtd),"ticket_medio":ticket,
          "pago_qtd":int(len(pago)),"pago_val":round(float(pago["valor"].sum()),2),
          "org_qtd": int(len(org)), "org_val": round(float(org["valor"].sum()),2),
          "por_produto": por_produto}
    agg=df.groupby("date").agg(qtd=("valor","count"),valor=("valor","sum")).reset_index().sort_values("date")
    daily={"days":[r["date"].strftime("%d/%m") for _,r in agg.iterrows()],
           "qtd": [int(r["qtd"]) for _,r in agg.iterrows()],
           "valor":[round(float(r["valor"]),2) for _,r in agg.iterrows()]}
    # Raw por linha — para filtro de período e produto correto no JS
    _pc = prod_col2 if 'prod_col2' in vars() else next((c for c in df.columns if "produto" in c.lower()),"Produto")
    _oc = orig_col2 if 'orig_col2' in vars() else next((c for c in df.columns if "organico" in c.lower() or "orgân" in c.lower() or "pago" in c.lower()),"Organico ou Pago")
    raw = []
    for _, r in df.iterrows():
        d = r["date"]
        if pd.isna(d): continue
        raw.append({
            "d": d.strftime("%d/%m"),
            "p": str(r[_pc]),
            "v": round(float(r["valor"]),2),
            "t": str(r[_oc])
        })
    return kpis, daily, raw

# ══ META ADS ══════════════════════════════════════════
def load_meta():
    print("  Lendo meta-ads...")
    df=pd.read_csv(URL_META)
    df=df.rename(columns={"Date":"date","Campaign Name":"campaign","Adset Name":"adset",
        "Ad Name":"ad","Thumbnail URL":"thumb","Status":"status","Spend (Cost, Amount Spent)":"spend",
        "Impressions":"impressions","Action Link Clicks":"link_clicks",
        "Action Landing Page View":"page_view","Action Omni Initiated Checkout":"init_checkout",
        "Action Omni Purchase":"purchase","Action Value Omni Purchase":"revenue_meta"})
    df["date"]=pd.to_datetime(df["date"],errors="coerce")
    for c in ["spend","impressions","link_clicks","page_view","init_checkout","purchase","revenue_meta"]:
        if c in df.columns: df[c]=to_num(df[c])
    if "status" not in df.columns: df["status"]=""
    df["is_lct"]=df["campaign"].str.contains(LANCAMENTO_COD,na=False,case=False) if LANCAMENTO_COD else True
    df=df.dropna(subset=["date"])
    print(f"     {len(df)} linhas | {df['date'].min().date()} → {df['date'].max().date()}")
    return df

def calc_kpis(p, ticket):
    sp=float(p["spend"].sum()); imp=float(p["impressions"].sum())
    lc=float(p["link_clicks"].sum()); pv=float(p["page_view"].sum())
    ic=float(p["init_checkout"].sum()); pur=float(p["purchase"].sum())
    rev=pur*ticket  # receita correta = purchase × ticket_medio
    return {"spend":round(sp,2),"impressions":int(imp),"link_clicks":int(lc),
            "page_view":int(pv),"init_checkout":int(ic),"purchase":int(pur),
            "revenue":round(rev,2),
            "ctr":   round(lc/imp*100,2) if imp>0 else None,
            "connect_rate":round(pv/lc*100,2) if lc>0 else None,
            "tx_ic": round(ic/pv*100,2) if pv>0 else None,
            "tx_checkout":round(pur/ic*100,2) if ic>0 else None,
            "tx_conv":round(pur/pv*100,2) if pv>0 else None,
            "cpa":   round(sp/pur,2) if pur>0 else None,
            "roas":  round(rev/sp,2) if sp>0 else None,
            "cpm":   round(sp/imp*1000,2) if imp>0 else None}

def meta_kpis(df, ticket):
    return {"lct":calc_kpis(df[df["is_lct"]],ticket),"all":calc_kpis(df,ticket)}

def build_daily(p, ticket):
    agg=p.groupby("date").agg(spend=("spend","sum"),impressions=("impressions","sum"),
        link_clicks=("link_clicks","sum"),page_view=("page_view","sum"),
        init_checkout=("init_checkout","sum"),purchase=("purchase","sum")
    ).reset_index().sort_values("date")
    out={k:[] for k in ["days","spend","impressions","link_clicks","page_view",
                         "init_checkout","purchase","revenue","ctr","connect_rate",
                         "tx_ic","tx_checkout","tx_conv","cpa","roas","cpm"]}
    for _,r in agg.iterrows():
        sp=float(r["spend"]); imp=float(r["impressions"]); lc=float(r["link_clicks"])
        pv=float(r["page_view"]); ic=float(r["init_checkout"]); pur=float(r["purchase"])
        rev=pur*ticket
        out["days"].append(r["date"].strftime("%d/%m"))
        out["spend"].append(round(sp,2)); out["impressions"].append(int(imp))
        out["link_clicks"].append(int(lc)); out["page_view"].append(int(pv))
        out["init_checkout"].append(int(ic)); out["purchase"].append(int(pur))
        out["revenue"].append(round(rev,2))
        out["ctr"].append(round(lc/imp*100,2) if imp>0 else None)
        out["connect_rate"].append(round(pv/lc*100,2) if lc>0 else None)
        out["tx_ic"].append(round(ic/pv*100,2) if pv>0 else None)
        out["tx_checkout"].append(round(pur/ic*100,2) if ic>0 else None)
        out["tx_conv"].append(round(pur/pv*100,2) if pv>0 else None)
        out["cpa"].append(round(sp/pur,2) if pur>0 else None)
        out["roas"].append(round(rev/sp,2) if sp>0 else None)
        out["cpm"].append(round(sp/imp*1000,2) if imp>0 else None)
    return out

def meta_daily(df, ticket):
    return {"lct":build_daily(df[df["is_lct"]],ticket),"all":build_daily(df,ticket)}

def meta_daily_camps(df, ticket):
    """Daily por campanha para filtro nas métricas diárias"""
    result={"lct":{},"all":{}}
    for key,subset in [("lct",df[df["is_lct"]]),("all",df)]:
        for camp in subset["campaign"].unique():
            p=subset[subset["campaign"]==camp]
            result[key][camp]=build_daily(p,ticket)
    return result

def meta_raw(df, ticket):
    """Raw agregado por dia+campanha+adset — para filtro de datas livres nas tabelas"""
    rows=[]
    agg=df.groupby(["date","campaign","adset","is_lct"]).agg(
        spend=("spend","sum"), purchase=("purchase","sum"),
        impressions=("impressions","sum"), link_clicks=("link_clicks","sum"),
        page_view=("page_view","sum"), init_checkout=("init_checkout","sum"),
        revenue_meta=("revenue_meta","sum")
    ).reset_index()
    for _,r in agg.iterrows():
        sp=float(r["spend"]); pur=int(r["purchase"]); imp=int(r["impressions"])
        lc=int(r["link_clicks"]); pv=int(r["page_view"]); ic=int(r["init_checkout"])
        rev=float(r["revenue_meta"])
        rows.append({
            "d": r["date"].strftime("%d/%m"),
            "c": str(r["campaign"]),
            "a": str(r["adset"]),
            "lct": bool(r["is_lct"]),
            "sp": round(sp,2), "pur": pur, "imp": imp,
            "lc": lc, "pv": pv, "ic": ic, "rev": round(rev,2)
        })
    return rows

def build_rows(agg, col, ticket):
    rows=[]
    for _,r in agg.sort_values("purchase",ascending=False).iterrows():
        sp=float(r["spend"]); imp=float(r["impressions"]); lc=float(r["link_clicks"])
        pv=float(r["page_view"]); ic=float(r["init_checkout"]); pur=float(r["purchase"])
        rev=float(r.get("revenue_meta",0) or pur*ticket)  # usa Action Value se disponível
        rows.append({"n":str(r[col]),"st":str(r.get("status","") or ""),"spend":round(sp,2),"imp":int(imp),"lc":int(lc),
            "pv":int(pv),"ic":int(ic),"pur":int(pur),"rev":round(rev,2),
            "ctr":round(lc/imp*100,2) if imp>0 else None,
            "cr": round(pv/lc*100,2)  if lc>0 else None,
            "tx_ic":round(ic/pv*100,2) if pv>0 else None,
            "tx_ck":round(pur/ic*100,2) if ic>0 else None,
            "tx_cv":round(pur/pv*100,2) if pv>0 else None,
            "cpa":round(sp/pur,2) if pur>0 else None,
            "roas":round(rev/sp,2) if sp>0 else None,
            "cpm":round(sp/imp*1000,2) if imp>0 else None})
    return rows

def meta_tables_period(df, p, img_dir, ticket):
    """Calcula tabelas para um subset p do df"""
    def ag(sub,col):
        return sub.sort_values("date").groupby(col).agg(spend=("spend","sum"),impressions=("impressions","sum"),
            link_clicks=("link_clicks","sum"),page_view=("page_view","sum"),
            init_checkout=("init_checkout","sum"),purchase=("purchase","sum"),
            revenue_meta=("revenue_meta","sum"),status=("status","last")).reset_index()
    def make(sub,col): return build_rows(ag(sub,col),col,ticket)
    def make_adsets(sub):
        agg2=sub.sort_values("date").groupby(["campaign","adset"]).agg(spend=("spend","sum"),impressions=("impressions","sum"),
            link_clicks=("link_clicks","sum"),page_view=("page_view","sum"),
            init_checkout=("init_checkout","sum"),purchase=("purchase","sum"),
            revenue_meta=("revenue_meta","sum"),status=("status","last")).reset_index()
        rows=[]
        for _,r in agg2.sort_values("purchase",ascending=False).iterrows():
            sp=float(r["spend"]); imp=float(r["impressions"]); lc=float(r["link_clicks"])
            pv=float(r["page_view"]); ic=float(r["init_checkout"]); pur=float(r["purchase"])
            rev=float(r.get("revenue_meta",0) or pur*ticket)
            rows.append({"n":str(r["adset"]),"camp":str(r["campaign"]),"st":str(r.get("status","") or ""),"spend":round(sp,2),
                "imp":int(imp),"lc":int(lc),"pv":int(pv),"ic":int(ic),"pur":int(pur),"rev":round(rev,2),
                "ctr":round(lc/imp*100,2) if imp>0 else None,
                "cr": round(pv/lc*100,2)  if lc>0 else None,
                "tx_ic":round(ic/pv*100,2) if pv>0 else None,
                "tx_ck":round(pur/ic*100,2) if ic>0 else None,
                "tx_cv":round(pur/pv*100,2) if pv>0 else None,
                "cpa":round(sp/pur,2) if pur>0 else None,
                "roas":round(rev/sp,2) if sp>0 else None,
                "cpm":round(sp/imp*1000,2) if imp>0 else None})
        return rows
    # Mapa de thumb: ad+adset+camp → url (do df completo, não só do período)
    df_full_thumb=df[df["thumb"].notna()&(df["thumb"].astype(str)!="nan")]
    thumb_map={}
    for _,r in df_full_thumb.iterrows():
        k=(str(r["ad"]),str(r["adset"]),str(r["campaign"]))
        if k not in thumb_map:
            thumb_map[k]=download_thumb(str(r["thumb"]),img_dir)

    def make_ads(sub):
        # Agregar métricas do período, buscar thumb do mapa completo
        agg=sub.groupby(["ad","adset","campaign"]).agg(spend=("spend","sum"),impressions=("impressions","sum"),
            link_clicks=("link_clicks","sum"),purchase=("purchase","sum")).reset_index().sort_values("purchase",ascending=False)
        if agg.empty: return []
        ads=[]
        for _,r in agg.iterrows():
            sp=float(r["spend"]); imp=float(r["impressions"]); lc=float(r["link_clicks"]); pur=float(r["purchase"])
            k=(str(r["ad"]),str(r["adset"]),str(r["campaign"]))
            ads.append({"n":str(r["ad"]),"adset":str(r["adset"]),"camp":str(r["campaign"]),
                "thumb":thumb_map.get(k,""),
                "spend":round(sp,2),"pur":int(pur),"imp":int(imp),"lc":int(lc),
                "ctr":round(lc/imp*100,2) if imp>0 else None,
                "cpa":round(sp/pur,2) if pur>0 else None})
        return ads
    return {"camps":make(p,"campaign"),"adsets":make_adsets(p),"ads":make_ads(p)}

def meta_tables(df, img_dir, ticket):
    """Exporta tabelas por período: 1d,7d,14d,30d,all — baseado na data de geração"""
    from datetime import timezone, timedelta
    hoje=pd.Timestamp(date.today())  # data de geração como referência
    result={"lct":{},"all":{}}
    periods={"1":1,"7":7,"14":14,"30":30,"all":0}
    for key,subset in [("lct",df[df["is_lct"]]),("all",df)]:
        for pname,n in periods.items():
            p=subset[subset["date"]>=hoje-pd.Timedelta(days=n-1)] if n>0 else subset
            result[key][pname]=meta_tables_period(df,p,img_dir,ticket)
            print(f"     [{key}][{pname}]: {len(result[key][pname]['camps'])} camps")
    return result

def meta_breakdowns(df):
    print("  Lendo breakdowns...")
    hoje_bd=pd.Timestamp(date.today())  # referência = data de geração
    last=hoje_bd  # usar hoje como limite superior
    AGE_ORDER=["18-24","25-34","35-44","45-54","55-64","65+"]
    def seg(agg,dim):
        agg=agg[agg["spend"]>0].copy()
        agg["cpa"]=(agg["spend"]/agg["purchase"]).where(agg["purchase"]>0).round(2)
        return [{"n":str(r[dim]),"spend":round(float(r["spend"]),2),"pur":int(r["purchase"]),"cpa":safe(r["cpa"])} for _,r in agg.iterrows()]
    try:
        df_ga=pd.read_csv(URL_GA)
        df_ga["date"]=pd.to_datetime(df_ga["Date"],errors="coerce")
        df_ga["spend"]=to_num(df_ga["Spend (Cost, Amount Spent)"])
        df_ga["purchase"]=to_num(df_ga["Action Omni Purchase"])
        df_ga["age"]=df_ga["Age (Breakdown)"].astype(str)
        df_ga["gender"]=df_ga["Gender (Breakdown)"].astype(str)
        df_ga=df_ga.dropna(subset=["date"])
    except Exception as e: print(f"  Aviso GA: {e}"); df_ga=pd.DataFrame()
    try:
        df_pt=pd.read_csv(URL_PT)
        df_pt["date"]=pd.to_datetime(df_pt["Date"],errors="coerce")
        df_pt["spend"]=to_num(df_pt["Spend (Cost, Amount Spent)"])
        df_pt["purchase"]=to_num(df_pt["Action Omni Purchase"])
        df_pt["platform"]=df_pt["Platform Position (Breakdown)"].astype(str)
        df_pt=df_pt.dropna(subset=["date"])
    except Exception as e: print(f"  Aviso PT: {e}"); df_pt=pd.DataFrame()

    result={}
    for pname,n in [("1",1),("7",7),("14",14),("30",30),("all",0)]:
        if n>0:
            start=hoje_bd-pd.Timedelta(days=n-1)
            pga=df_ga[(df_ga["date"]>=start)&(df_ga["date"]<=hoje_bd)] if len(df_ga)>0 else df_ga
            ppt=df_pt[(df_pt["date"]>=start)&(df_pt["date"]<=hoje_bd)] if len(df_pt)>0 else df_pt
        else:
            pga=df_ga; ppt=df_pt
        if len(pga)>0:
            ag_age=pga[pga["age"].isin(AGE_ORDER)].groupby("age").agg(spend=("spend","sum"),purchase=("purchase","sum")).reset_index()
            ag_age["_o"]=ag_age["age"].apply(lambda x:AGE_ORDER.index(x) if x in AGE_ORDER else 99)
            age_d=seg(ag_age.sort_values("_o"),"age")
            ag_gen=pga[pga["gender"].isin(["female","male"])].groupby("gender").agg(spend=("spend","sum"),purchase=("purchase","sum")).reset_index().sort_values("purchase",ascending=False)
            gen_d=seg(ag_gen,"gender")
        else: age_d=[]; gen_d=[]
        if len(ppt)>0:
            ag_pt=ppt.groupby("platform").agg(spend=("spend","sum"),purchase=("purchase","sum")).reset_index().sort_values("purchase",ascending=False).head(8)
            plat_d=seg(ag_pt,"platform")
        else: plat_d=[]
        result[pname]={"age":age_d,"gender":gen_d,"platform":plat_d}
    # Também exportar raw por dia para filtro de datas livres
    raw_ga=[]
    if len(df_ga)>0:
        for _,r in df_ga.iterrows():
            if pd.isna(r['date']): continue
            raw_ga.append({'d':r['date'].strftime('%d/%m'),'age':str(r['age']),'gen':str(r['gender']),
                           'sp':round(float(r['spend']),2),'pur':int(r['purchase'])})
    raw_pt=[]
    if len(df_pt)>0:
        for _,r in df_pt.iterrows():
            if pd.isna(r['date']): continue
            raw_pt.append({'d':r['date'].strftime('%d/%m'),'plat':str(r['platform']),
                           'sp':round(float(r['spend']),2),'pur':int(r['purchase'])})
    result['_raw_ga']=raw_ga
    result['_raw_pt']=raw_pt
    return result

# ══ PESQUISA ══════════════════════════════════════════
def load_pesquisa():
    print("  Lendo pesquisa..."); return pd.read_csv(URL_PES)

def pesquisa_process(df, hot_qtd):
    # Perguntas dinâmicas: todas as colunas que NÃO são UTM nem de controle
    UTM_COLS=["utm_source","utm_medium","utm_campaign","utm_content"]
    SKIP_COLS=set(UTM_COLS+["Carimbo de data/hora","Timestamp","Email","email",
                             "Nome","nome","ID","id","Unnamed: 0"])
    # Considerar como pergunta qualquer coluna com texto longo (provável questão)
    PERGUNTAS=[c for c in df.columns
               if c not in SKIP_COLS
               and not c.lower().startswith("unnamed")
               and pd.api.types.is_string_dtype(df[c])  # aceita str e object
               and df[c].nunique() <= 50] # não é ID único por linha
    graficos=[]
    for p in PERGUNTAS:
        if p not in df.columns: continue
        vc=df[p].value_counts(); total=vc.sum()
        graficos.append({"pergunta":p,"opcoes":[{"label":str(k),"qtd":int(v),"pct":round(v/total*100,1)} for k,v in vc.items()]})
    filtros={}
    for col in UTM_COLS:
        if col in df.columns:
            filtros[col]=sorted([v for v in df[col].dropna().unique().tolist() if v and str(v)!="nan"])
    rows=[]
    for _,r in df.iterrows():
        row={}
        for p in PERGUNTAS: row[p]=str(r[p]) if p in df.columns and pd.notna(r.get(p)) else None
        for col in UTM_COLS: row[col]=str(r[col]) if col in df.columns and pd.notna(r.get(col)) else None
        rows.append(row)
    return {"total":len(df),"hot_qtd":int(hot_qtd),"graficos":graficos,"filtros":filtros,"rows":rows,"perguntas":PERGUNTAS}

# ══ INJEÇÃO ════════════════════════════════════════════
def replace_js_const(html, name, value):
    pattern=rf"const {name}\s*=\s*(?:null|true|false|-?\d[\d\.]*|'[^']*'|\"[^\"]*\"|\{{[\s\S]*?\}}|\[[\s\S]*?\])\s*;"
    replacement=f"const {name} = {json.dumps(value,ensure_ascii=False)};"
    # Usar lambda para evitar interpretação de \ no replacement
    found=[0]
    def do_replace(m):
        found[0]+=1
        return replacement
    new_html=re.sub(pattern,do_replace,html,count=1)
    if not found[0]: print(f"  AVISO: não encontrou const {name}")
    return new_html

def inject_all(tpl, meta_k, meta_d, meta_dc, meta_raw_c, meta_t, meta_bd, hot_k, hot_d, hot_raw, pes, ticket):
    html=Path(tpl).read_text(encoding="utf-8")
    html=replace_js_const(html,"META_KPIS",    meta_k)
    html=replace_js_const(html,"META_DAILY",       meta_d)
    html=replace_js_const(html,"META_DAILY_CAMPS", meta_dc)
    html=replace_js_const(html,"META_RAW_CAMP",    meta_raw_c)
    html=replace_js_const(html,"META_TABLES",      meta_t)
    html=replace_js_const(html,"META_BD",      meta_bd)
    html=replace_js_const(html,"HOT_KPIS",     hot_k)
    html=replace_js_const(html,"HOT_DAILY",    hot_d)
    html=replace_js_const(html,"HOT_RAW",      hot_raw)
    html=replace_js_const(html,"PESQUISA", pes if USAR_PESQUISA else False)
    html=replace_js_const(html,"TICKET_MEDIO", ticket)
    # Data de geração em Brasília (UTC-3) para o filtro de período correto
    from datetime import timezone, timedelta
    brt = timezone(timedelta(hours=-3))
    hoje_brt = date.today()  # data local do servidor (GitHub Actions = UTC, mas usamos a data atual)
    html=replace_js_const(html,"DATA_GERACAO", hoje_brt.strftime("%Y-%m-%d"))
    for k,v in [("LANCAMENTO_COD",f"'{LANCAMENTO_COD}'"),("NOME_CLIENTE",f"'{NOME_CLIENTE}'"),
                ("USAR_ORIGEM","true" if USAR_ORIGEM else "false"),
                ("LOGO_LETRA",f"'{LOGO_LETRA}'"),("COR_ACENTO",f"'{COR_ACENTO}'"),
                ("CPA_BOM",str(CPA_BOM)),("CPA_MEDIO",str(CPA_MEDIO)),
                ("ROAS_BOM",str(ROAS_BOM)),("ROAS_MEDIO",str(ROAS_MEDIO)),
                ("CTR_BOM",str(CTR_BOM)),("CTR_MEDIO",str(CTR_MEDIO)),
                ("CR_BOM",str(CR_BOM)),("CR_MEDIO",str(CR_MEDIO)),
                ("TX_IC_BOM",str(TX_IC_BOM)),("TX_IC_MEDIO",str(TX_IC_MEDIO)),
                ("TX_CK_BOM",str(TX_CK_BOM)),("TX_CK_MEDIO",str(TX_CK_MEDIO)),
                ("TX_CONV_BOM",str(TX_CONV_BOM)),("TX_CONV_MEDIO",str(TX_CONV_MEDIO)),
                ("CPM_BOM",str(CPM_BOM)),("CPM_MEDIO",str(CPM_MEDIO))]:
        html=re.sub(rf"const {k}\s*=\s*[^;]+;",f"const {k}={v};",html,count=1)
    html=re.sub(r"\d{2}/\d{2}/\d{4} · via planilha",date.today().strftime("%d/%m/%Y")+" · via planilha",html)
    return html

# ══ MAIN ═══════════════════════════════════════════════
def main():
    print("="*60)
    print(f"Dashboard Lançamento — {NOME_CLIENTE} / {LANCAMENTO_COD or 'Todos'}")
    print("="*60)
    img_dir=Path("imgs"); img_dir.mkdir(exist_ok=True)

    print("\n[EDUZZ]")
    df_hot=load_hotmart()
    hot_k,hot_d,h_raw=hotmart_process(df_hot)
    ticket=hot_k["ticket_medio"]
    print(f"  ✓ {hot_k['qtd']} vendas | R$ {hot_k['total']:,.2f} | ticket R$ {ticket:.2f}")

    print("\n[META ADS]")
    df_meta=load_meta()
    m_k=meta_kpis(df_meta,ticket)
    m_d=meta_daily(df_meta,ticket)
    m_dc=meta_daily_camps(df_meta,ticket)
    m_raw=meta_raw(df_meta,ticket)
    m_t=meta_tables(df_meta,img_dir,ticket)
    m_bd=meta_breakdowns(df_meta)
    print(f"  ✓ {len(m_t['lct']['all']['camps'])} camps | {len(m_t['lct']['all']['adsets'])} adsets | {len(m_t['lct']['all']['ads'])} ads")

    print("\n[PESQUISA]")
    df_pes=load_pesquisa()
    pes=pesquisa_process(df_pes, hot_k["qtd"])
    print(f"  ✓ {pes['total']} respostas")

    print("\n[HTML]")
    if not Path(TEMPLATE_FILE).exists():
        print(f"  ERRO: {TEMPLATE_FILE} não encontrado"); return
    html=inject_all(TEMPLATE_FILE,m_k,m_d,m_dc,m_raw,m_t,m_bd,hot_k,hot_d,h_raw,pes,ticket)
    Path(OUTPUT_FILE).write_text(html,encoding="utf-8")
    print(f"  ✓ {OUTPUT_FILE} ({len(html)//1024}KB)")

    data_json={"cliente":NOME_CLIENTE,"cor":COR_ACENTO,"letra":LOGO_LETRA,
               "lancamento":LANCAMENTO_COD,"atualizado":date.today().strftime("%d/%m/%Y"),
               "kpis":{"spend":m_k["lct"].get("spend"),"vendas":hot_k.get("qtd"),
                       "faturamento":hot_k.get("total"),"cpa":m_k["lct"].get("cpa")}}
    Path("data.json").write_text(json.dumps(data_json,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"  ✓ data.json\n{'='*60}")

if __name__=="__main__":
    main()
