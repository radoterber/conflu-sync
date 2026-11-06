# Confluence ↔ Markdown – nástroje

Samostatná Python miniaplikace pro export stránek z Confluence do Markdownu a publikaci Markdownu
zpět do Confluence. Je to obecný enabler, nesouvisí s obsahem konkrétního projektu.

| Soubor | Účel |
|---|---|
| `extract-from-confluence.py` | export stránky / stromu stránek do `.md` + příloh |
| `publish-to-confluence.py` | publikace `.md` (souboru / stromu) do Confluence |
| `confluence_lib.py` | sdílené: REST klient, parsování URL, kódování konzole, cesty, front matter |
| `requirements-confluence.txt` | závislosti (`pip install -r scripts/requirements-confluence.txt`) |

Autentizace: Personal Access Token v proměnné `CONFLUENCE_PAT` (nebo `--pat`).
Na interní instanci obvykle `--skip-tls-check`.
Podrobný popis parametrů: `python scripts/<skript>.py --help` a hlavičky obou skriptů.

## Použití

```powershell
# export celého stromu (adresářová struktura = hierarchie stránek)
python scripts/extract-from-confluence.py --page "https://confluence.jtfg.com/x/u5cBCQ" `
    --output-dir docs/confluence --tree --skip-tls-check

# náhled publikace (nic nemění)
python scripts/publish-to-confluence.py --source-dir docs/confluence --recursive `
    --parent-page "https://confluence.jtfg.com/x/..." --dry-run --skip-tls-check

# publikace jednoho souboru (ID stránky z front matter)
python scripts/publish-to-confluence.py --source-file "docs/confluence/X/X.md" --skip-tls-check
```

`--page` / `--parent-page` přijímá: `/x/TINY`, `/spaces/S/pages/N/…`, `viewpage.action?pageId=N`,
`/display/S/Title`, `/pages/N/…`, nebo číselné ID s `--confluence-url`.

## Formát Markdownu

Každý soubor začíná front matter s metadaty stránky (`title`, `confluence_page_id`, `space`,
`version`, `parent_id`, `url`, z extrakce i `last_modified`). Přílohy jsou v `img/` vedle souboru.

| Prvek Confluence | Markdown |
|---|---|
| úkol | `- [ ] text` / `- [x] text` |
| status | `{{status:Green:DONE}}` |
| datum | `{{date:2026-02-27}}` |
| sloučené buňky tabulky | `{{span:up}}` / `{{span:left}}` |
| zmínka uživatele | `[@Jméno](confluence-user:USERKEY)` |
| info/note/tip/warning | `> [!NOTE] titulek` |
| ostatní makra | `<!-- confluence: <ac:structured-macro …> -->` (původní XML, vratné) |
| makra s tělem | `<!-- confluence-begin: … -->` markdown `<!-- confluence-end: name -->` |
| odkaz na stránku ve stromu | relativní odkaz na `.md` |
| odkaz na stránku mimo strom | absolutní URL |

## Chování publikace

Nejprve se převedou všechny soubory (známé odkazy i přílohy jiných stránek), pak se publikuje
od rodičů k potomkům.

| Situace | Chování |
|---|---|
| Nový `.md` bez `confluence_page_id` | nová stránka pod stránkou nadřazeného souboru (`X/X.md`), jinak pod `--parent-page` |
| Stránka se stejným názvem existuje **jinde** ve space | **CONFLICT**, nepřepíše se (`--adopt` / `--force` pro převzetí) |
| Stránka se stejným názvem pod očekávaným rodičem | aktualizuje se |
| `confluence_page_id` ukazuje na smazanou stránku | upozornění, vyhledání podle názvu / vytvoření nové |
| Verze v souboru ≠ verze v Confluence | CONFLICT (`--force`) |
| Rodič selhal | potomci se přeskočí (chyba), nevzniknou na špatném místě |
| Obsah se nezměnil | stránka se neaktualizuje (žádná nová verze) |
| Nová / změněná příloha | nahraje se; SHA-256 se uloží do komentáře přílohy |
| Nezměněná příloha | přeskočí se – porovnává se SHA-256 z komentáře; u příloh bez něj se příloha stáhne a porovná obsah. `fileSize` z Confluence se nepoužívá (není spolehlivý). |
| Příloha z `img/` jiné stránky téhož běhu | nahraje se k té stránce, odkaz vede na ni |
| Příloha jiné stránky mimo běh | nenahraje se, varování |
| Dva různé soubory se stejným jménem na jedné stránce | chyba, stránka se nepublikuje |
| Selhání nahrání přílohy | chyba, návratový kód 1 |
| Lokálně chybějící soubor | varování |
| Smazaná lokální příloha / soubor | v Confluence zůstává (skript nic nemaže) |
| `--source-file` bez ID | s `--parent-page` vytvoří novou stránku (kolize názvu → CONFLICT), jinak chyba |
| `--dry-run` | ukáže CREATE / UPDATE / keep a stav příloh (new / changed / unchanged) |

### Zpětný zápis metadat (výchozí, vypnutí `--no-write-back`)

Po vytvoření/aktualizaci stránky se do front matter zapíše `confluence_page_id`, `space`, `version`,
`url` (u nových souborů i `parent_id`). Další běhy pak nezávisí na názvu a nehlásí falešné konflikty.
`last_modified` se nezapisuje (zastaralá hodnota z extrakce se při změně verze odstraní).

| Situace | Zápis |
|---|---|
| nový soubor bez ID | ano |
| soubor patří publikované stránce, verze se změnila | ano |
| stránka beze změny | ne (soubor se nemění) |
| soubor patří jiné stránce / jinému space (publikace kopie) | ne, jen informace |
| `--title-prefix` | ne |
| `--dry-run`, `--no-write-back` | ne |

`--write-back` zůstává jako alias kvůli kompatibilitě. Publikace mění zdrojové `.md` soubory –
změny `version` je vhodné commitovat spolu s obsahem.

## Známá omezení

- Barvy, šířky sloupců a layout sloupců se do Markdownu nepřenáší (při publikaci zpět se ztratí).
- Jména u zmínek uživatelů a texty odkazů jsou v Markdownu informativní; Confluence je odvozuje sám.
- Odkazy na stránky mimo exportovaný strom jsou absolutní URL.
- Přílohy bez uloženého SHA-256 se při publikaci stahují kvůli porovnání (jen poprvé).
- Windows: cesty > 260 znaků se řeší prefixem `\\?\` – běžné nástroje (PowerShell `Get-Content`,
  `Select-String`) s nimi mohou mít potíže; v konzoli cp1250 se diakritika ve výpisu může zobrazit
  chybně (soubory jsou v pořádku, UTF-8).

## Historie: kontrola a oprava (2026-11-05/06)

### Chyby v původních skriptech

| # | Problém | Dopad |
|---|---|---|
| E1 | Výpis znaků mimo cp1250 (`─`, emoji) do konzole Windows | extrakce padala na **všech** stránkách |
| E2 | mistune převáděl názvy obrázků do URL tvaru (`Sn%C3%ADmek%20...`) | nesoulad s názvy příloh, rozbité obrázky |
| E3 | Cesty s mezerou jako `![](img/a b.png)` | mistune je nepozná jako obrázek |
| E4 | Čtení bez tolerance BOM | špatně zjištěný titulek |
| X1 | Mazání **všech** prvků `ac:*` i s obsahem | ztráta 58 % textu (layouty, úkoly, komentované pasáže) |
| X2 | Nepodporovaná makra se zahodila | ztráta informace |
| X3 | Nepodporovány tiny linky `/x/...` | |
| X4 | Cesty > 260 znaků, názvy končící tečkou | chyby zápisu, rozbité relativní odkazy |
| P3 | `escape=False` – raw HTML z Markdownu do storage | nevalidní XHTML |
| P2/P4 | Nepodporována hierarchie, odkazy mezi `.md`, úkoly | |
| P5–P11 | Přepis cizí stránky se stejným názvem, potomci na špatném místě, opakované nahrávání příloh, kolize jmen příloh, chyby příloh jen jako varování, nelze vytvořit stránku z jednoho souboru | |

Čtení z REST API (UTF-8 JSON) i zápis `.md` v UTF-8 fungovaly správně.

### Ověření

Na space PDOT (262 stránek, 864 příloh) a v osobním space `~JT0024785` pod stránkou „AI Pokusy“:

| Test | Výsledek |
|---|---|
| Export 262 stránek | 0 chyb |
| Ztráta textu storage → Markdown | 0 z 168 020 slov |
| Diakritika / U+FFFD / mojibake | nenalezeno |
| Relativní odkazy | 0 rozbitých |
| Offline roundtrip storage → MD → storage | 100 % validní XHTML, makra zachována |
| Publikace podstromu (12 stránek + obrázky) a re-export | text a tabulky shodné; rozdíly jen odkazy mimo podmnožinu a kosmetika |
| Nový strom, kolize příloh, přeskočení potomků, kolize názvu, opakovaný běh bez změn, změna přílohy, příloha cizí stránky, přejmenování, konflikt verzí, smazaná stránka, jeden soubor | vše dle tabulky chování |
| Zpětný zápis metadat (nové soubory, opakované úpravy, kopie s prefixem, cizí ID, `--no-write-back`) | dle tabulky zápisu |
| Přílohy s chybným `fileSize` v Confluence | vyhodnoceny jako nezměněné |
| Krátké tiny linky (např. `/x/kohXB`, 5 znaků) | opraveno 2026-11-06: Confluence odřezává koncová `A`, dekódování je doplňuje |

Testovací stránky `[TEST] …` a `[T2] …` pod „AI Pokusy“ (https://confluence.jtfg.com/x/75WZEQ)
zůstaly pro kontrolu; lze je smazat.
