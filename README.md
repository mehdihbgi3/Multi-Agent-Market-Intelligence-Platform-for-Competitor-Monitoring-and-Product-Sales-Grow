# Borna Pricing & Market Intelligence Platform

**A multi-agent web platform that keeps Borna Sensor's prices in step with the dollar, watches competitors, and helps the sales team sell more.**

![python](https://img.shields.io/badge/python-3.10%2B-3776ab)
![flask](https://img.shields.io/badge/web-Flask%20%2B%20waitress-000000)
![database](https://img.shields.io/badge/database-SQLite-003b57)
![browser](https://img.shields.io/badge/browser-Playwright-2ead33)
![tests](https://img.shields.io/badge/tests-302%20passed%2C%201%20skipped-2ea44f)
![interface](https://img.shields.io/badge/interface-Persian%2C%20right--to--left-6f42c1)

![The dashboard: today's official price list, the free-market dollar and how far it has moved](docs/images/dashboard.png)

<sub>Every screenshot on this page comes from a demo copy of the system. The competitors, customers and businesses in them are made up.</sub>

A typeset LaTeX edition of this page is in [`docs/README.pdf`](docs/README.pdf) (source: [`docs/README.tex`](docs/README.tex)). The detailed user and installation guide is in Persian in [`README_fa.md`](README_fa.md), and [`CODE_GUIDE_fa.md`](CODE_GUIDE_fa.md) walks through the code file by file.

## Contents

1. [The problem](#the-problem)
2. [What the platform does](#what-the-platform-does)
3. [Who uses it and what they gain](#who-uses-it-and-what-they-gain)
4. [A working day](#a-working-day)
5. [How it works inside](#how-it-works-inside)
6. [The rules, written as formulas](#the-rules-written-as-formulas)
7. [A tour of every tab](#a-tour-of-every-tab)
8. [Getting started](#getting-started)
9. [Tests and quality](#tests-and-quality)
10. [Honest limits](#honest-limits)
11. [What is not in this repository](#what-is-not-in-this-repository)

---

## The problem

Borna Sensor makes and sells oxygen (lambda) sensors for cars in Iran: 27 products in its catalogue. Like most Iranian manufacturers, it prices against the free-market US dollar, and the dollar moves every day, sometimes every hour.

The pricing rule itself was settled long ago, but the work around it was manual. Someone had to look up the dollar, type it into an Excel workbook, let it recalculate and send a fresh list to the sales team, every time the market moved. Nobody had an organised view of what competitors were charging, why customers walked away, or which shops and garages might buy in bulk. Decisions were made on memory and gut feeling.

This platform turns all of that into one internal website. It sits on a server in the office, and everyone opens it in their own browser.

## What the platform does

**1. It prices on its own.** During working hours it fetches the free-market dollar every hour, checks that the number makes sense, recalculates all 27 prices with the approved formula and publishes the official list as a web page, a printable A4 sheet and an Excel file. Small moves go out automatically; big ones wait for the CEO or the finance manager. In testing, 108 prices were compared with the original Excel workbook and every one matched.

**2. It watches the market with a team of agents.** Once a day a scout agent searches Torob, Emalls, Digikala, Basalam and Divar for oxygen sensors, and reads OpenStreetMap for repair shops and parts stores across Iran. A matching agent decides which Borna product each listing really is. An analyst agent writes a weekly report. Every page that was read is kept as evidence, and suspicious prices are held back for a person to check.

**3. It compares fairly.** Competitors sell single sensors on retail sites and packs of ten or a hundred to the trade, so the platform works out the price of one piece and compares like with like: wholesale with wholesale, single units with single units.

**4. It gives sales a desk.** Customers, inquiries, orders, reasons for lost sales, a cheque-sale calculator, a pricing advisor, and a directory of potential customers (sellers, wholesalers, repair shops) with addresses and phone numbers, all exportable to Excel.

**5. It gives management one page** with the key numbers, everything waiting for a decision, and charts that show where Borna stands against the market.

And one thing it never does: no agent can change a Borna price or send anyone a message. Every decision that matters stays with a person, and every action is written to an event log.

## Who uses it and what they gain

| Who | How they use it | What they gain |
|---|---|---|
| Sales team | Open the dashboard, print or send the current list, log inquiries and orders, look up a competitor's price, find new customers. | Always the right price, no waiting for a new list, answers to "is our price fair?" and a ready list of businesses to call. |
| Finance manager and CEO | Approve large price changes and doubtful dollar rates, review matches and suspicious prices, read the advisor's suggestions. | Control without the busywork: only the decisions that need them reach them. |
| Management | Read the management page and the weekly report. | A clear picture of prices, competitors, sales and margins over 30, 90, 180 or 365 days. |
| The whole company | Works from one source of truth, in Persian, on the office network. | Fewer mistakes, a full history of every rate, list and decision, and evidence behind every competitor price. |

## A working day

1. **09:00, the rate.** The platform fetches the free-market dollar from priceto.day (tgju's data, free and keyless). The rate has to be fresh, must not jump more than 3% from the last accepted rate, and must sit inside a sane range. A rate that fails a check waits for an approver.
2. **Calculate and publish.** Once the rate is accepted, all 27 prices are recalculated. If any price changed, the whole list is published. Only when prices have moved more than 5% from the morning list does the CEO or the finance manager have to approve it.
3. **Sales works from the list.** Salespeople see the current list on the dashboard, print it or send the Excel file, and log inquiries and orders.
4. **During the day** the rate is fetched again every hour. The scout goes out once a day. Matches the system wasn't sure about wait for someone to review them.
5. **17:00, the list expires** (13:00 on Thursdays; Fridays are closed). If no price changed, the current list is simply renewed.
6. **Saturdays at 10:00** the weekly market report is built and the pricing advisor prepares its suggestions.

## How it works inside

### The life of a competitor price

Every competitor price goes through the same steps:

**found** (by the scout, the price watcher, a manual entry or an uploaded page) → **kept as evidence** (the page itself, fingerprinted with SHA-256) → **read** (title, price, seller, city, pack size) → **checked** (dollar window, sudden jumps, outliers) → **matched** to a Borna product → **approved** (automatically when the match is strong, otherwise by a person) → **compared** on the overview, the product pages, the weekly report and the management page.

Matching is strict where it has to be. A different ECU, socket colour, wire length (more than 30 mm apart), number of wires or another Borna part number is a hard no. A different wire colour or position means "needs a person". Genuine and aftermarket parts are never treated as the same thing.

### The eight agents

An agent is a small program with one job. A supervisor wakes up every 60 seconds and runs whichever agent is due. Any agent can also be started by hand.

![The agent centre: one card per agent, with its job, its permissions, its last run and a run-now button](docs/images/agents.png)

| Agent | Level | Job | When | Where you see the result |
|---|---|---|---|---|
| Price watcher | L0 | Reads registered and approved shop pages, keeps each page as evidence, records prices, quarantines suspicious ones | When each source is due | Listings, Overview, Price review |
| Market scout | L0 | Searches the five sites, reads the map, records sellers' prices and new businesses | Every 24 hours; the map every 7 days | Agent run log, Listings, Match review, Potential customers |
| Product matcher | L1 | Proposes a Borna product for each new listing, rejects hard mismatches, approves strong matches | Whenever there are new listings | Match review |
| Market analyst | L1 | Weekly report on Borna's position, competitor moves and new businesses | Saturdays 10:00 | Weekly report and its Excel |
| Pricing advisor | L2 | Suggests base-price changes from the wholesale median and a cost floor | After each weekly report | Pricing advisor; "waiting for you" |
| Wholesale lead finder | L1 | Keeps the businesses directory up to date and drops meaningless names | After new listings, at most every 6 hours | Potential customers |
| Lost-sale analyst | L1 | Suggests a reason for lost inquiries that have none | At most every 12 hours | The inquiry's page |
| Ad copy writer | L1 | Drafts listing text for Borna's own products | Only by hand | Ad copy |

**Levels.** L0 only reads public data. L1 writes internal data, and every write shows up in the event log. L2 proposes something sensitive, and a person decides.

### Technology

| Piece | Role |
|---|---|
| Python 3.10 or newer | The application, on Windows or Linux. Tested on 3.10 and 3.13. |
| Flask, Jinja2, waitress | Web pages and a production web server. |
| SQLite in WAL mode | A one-file database. A full backup is taken automatically before any change to its structure, and every day. |
| openpyxl | Reads the products workbook and writes every Excel file: price list, orders template, weekly report, advisor and businesses. |
| Playwright with Edge or Chrome | An invisible browser for JavaScript-heavy sites, using the browser already installed on the machine. |
| priceto.day, Alanchand | The free-market dollar rate. |
| Overpass (OpenStreetMap) | Repair shops and parts stores, free and keyless. |
| SVG charts and a bundled Persian font | Charts drawn by the server and type that work without internet access. |
| Any OpenAI-compatible model, optional | The writing helpers, for example Ollama on the company's own server. Without a model they work by rules. |
| pytest | 303 automated tests. |

```text
borna_pricing/            core: dollar rate, pricing engine, publishing, web app, Excel, charts
borna_pricing/market/     market intelligence: sources, fetcher, browser, scout, matching, reports, site check
borna_pricing/sales/      sales desk: customers, inquiries, orders, cheques, advisor, businesses
borna_pricing/ai/         model gateway and the writing helpers
borna_pricing/templates/  pages (Jinja2, right to left)
borna_pricing/static/     styles, scripts and the bundled font
tests/                    the test suite and its fixtures, including saved pages from every site
wheels/                   57 offline install packages, so installation needs no internet
data/                     database, backups, evidence and the products workbook (kept out of this repository)
```

### Security and good manners

- Passwords are stored with PBKDF2 and 600,000 iterations. Every form is protected against CSRF, and each role can only do its own work.
- Browsers only run the platform's own scripts. Reading the web can never reach the internal network, and saved competitor pages are only ever downloaded, never opened inside the app.
- The scout reads politely: it pauses between requests (15 seconds and three keywords per run for Torob), and the first "blocked" answer from a site (401, 403, 429, 490 or 503) stops that site until the next run. It never solves captchas, never logs in, and never presses a site's "show contact details" button.
- Phone numbers come only from the map (when the business listed one), from customer files or from a salesperson. Marketplace sellers' numbers are deliberately not collected.

## The rules, written as formulas

### Price of one product

Each product has a base price $P_0$ (rial) and a currency share $\alpha$ (1 by default). With today's dollar $S$ and the base rate $S_0$:

```math
P \;=\; P_0\left(\alpha\,\frac{S}{S_0} \;+\; 1 - \alpha\right)
```

The list shows $P$ rounded to the nearest 500,000 rial, half up, exactly like `ROUND` in Excel:

```math
P_{\text{list}} \;=\; 500\,000 \times \left\lfloor \frac{P}{500\,000} + \frac{1}{2} \right\rfloor
```

### When a new list needs a person

Rounding hides small moves and exaggerates others: one rounding step can be more than 2% of a mid-priced sensor. So the approval test uses the exact price before rounding, which is the effect of the dollar alone, compared with the first list of the day:

```math
\left|\,\frac{P(S_{\text{now}})}{P(S_{\text{morning}})} - 1\,\right| \;>\; 5\% \quad\Longrightarrow\quad \text{the CEO or the finance manager approves first}
```

Smaller changes are published on their own, and the whole list always goes out together.

### Checks on the dollar rate

| Check | Rule |
|---|---|
| Freshness | A rate older than 180 working minutes is rejected. |
| Jump | A move of more than 3% from the last accepted rate waits for an approver. |
| Range | Only rates between 100,000 and 2,000,000 toman are accepted. |
| Schedule | Every 60 minutes in working hours, retrying after 10 minutes on failure. A rate can also be entered by hand with its source. |
| Validity | A list is valid until 17:00 Saturday to Wednesday and 13:00 on Thursday. Fridays are closed. |

### Comparing with competitors

Everything is compared per piece. A listing for a pack of $q$ sensors at price $p_{\text{pack}}$ counts as

```math
u \;=\; \frac{p_{\text{pack}}}{q}
```

and lands on one side of the comparison:

```math
\text{side} \;=\;
\begin{cases}
\text{wholesale} & \text{if the seller says wholesale, or } q \ge 10\\
\text{single unit} & \text{otherwise}
\end{cases}
```

On each side, Borna's official per-piece price $P$ is compared with the median of the approved competitor prices $u_1, \dots, u_n$:

```math
g \;=\; \frac{P}{\mathrm{median}(u_1,\dots,u_n)} \;-\; 1, \qquad |g| \le 2\% \;\Rightarrow\; \text{"about the same"}
```

A side with fewer than three prices is labelled "thin data", so nobody reads too much into one or two numbers.

### Keeping bad prices out

A competitor price goes into quarantine, to be checked by a person, if any of these is true:

```math
u \;<\; 2\,\text{USD}\times S \quad\text{or}\quad u \;>\; 60\,\text{USD}\times S \qquad\text{(wrong unit, wrong pack size or a placeholder price)}
```

```math
\left|\,\frac{u_{\text{new}}}{u_{\text{previous}}} - 1\,\right| \;>\; 50\% \qquad\text{(sudden jump on the same listing)}
```

```math
\frac{u_i}{\mathrm{median}_{j \ne i}\; u_j} \;\notin\; [\,0.4,\; 2.5\,] \qquad\text{(outlier, once the product has at least three other prices on that side)}
```

Placeholder prices such as 1,000 toman, which some sellers post instead of "call me", fall under the first rule and never reach a comparison.

### The pricing advisor

With $m$ the median wholesale price of same-design competitors and a band $b = 5\%$:

```math
P_{\text{target}} \;=\;
\begin{cases}
m\,(1+b) & \text{if } P > m\,(1+b) \quad\text{(lower the base price)}\\
m\,(1-b) & \text{if } P < m\,(1-b) \quad\text{(room to raise it)}\\
P & \text{otherwise (keep)}
\end{cases}
```

If the dollar cost $c$ of a product and a minimum margin $\mu$ are recorded, no suggestion goes below the floor

```math
P_{\text{floor}} \;=\; \frac{c \cdot S}{1-\mu}
```

A suggestion never changes a price by itself. It is applied, if at all, through the normal Products page.

### Selling against a post-dated cheque

For a cash price $C$, a cheque due in $d$ days, an annual cost of money $r$ and an expected yearly rise of the dollar $\gamma$:

```math
f = (1+r)^{d/365}, \qquad K_{\min} = \left\lceil C\,f \right\rceil, \qquad K_{\mathrm{usd}} = \left\lceil C\,(1+\gamma)^{d/365} \right\rceil, \qquad \mathrm{PV}(K) = \frac{K}{f}
```

$K_{\min}$ is the smallest cheque worth taking, $K_{\mathrm{usd}}$ keeps the sale's dollar value, and $\mathrm{PV}(K)$ tells a salesperson what a customer's offered cheque $K$ is really worth today. Amounts round up to the next 10,000 rial.

## A tour of every tab

The interface is in Persian and reads right to left. Each tab's Persian name is given in brackets so you can find it on screen.

### Who sees what

| Role | What they can do |
|---|---|
| Sales (فروش) | Dashboard, price list, rate history and publications; in the market section: overview, listings, adding a competitor price, uploading a saved page, reading a price list and the weekly report; the whole sales section except the pricing advisor and Excel order import. They never see new prices before they are published. |
| Approver (تأییدکننده): finance or CEO | Everything sales can do, plus accepting or rejecting a rate, approving a list, the management page, match and price review, sources, agents, the pricing advisor and ad copy. |
| Admin (مدیر سامانه) | Everything, plus products, users, the event log, settings, new sources, the site check, pausing all agents and the AI status page. |

Login is by username and password. Five wrong passwords lock the login for five minutes. Everyone changes their own password from their name in the top bar.

### The main menu: eleven tabs

#### Dashboard (داشبورد)

The first page everyone sees (picture at the top of this page). From top to bottom:
- a box for a rate that is waiting for a decision (approvers only), with "accept and recalculate" and "reject";
- the official list's plate: list number, how long it is valid ("valid until 17:00 today"), when it was published, the dollar it was built on and who published it ("the system, automatically" or a person), with print and Excel buttons;
- today's free-market dollar: source, time, distance from the base rate and when the next fetch is due;
- a gap bar showing how far today's dollar is from the list's dollar. Yellow means the next list will go out on its own; red means it will need approval;
- rate and calculation tools for approvers: fetch now (at most once an hour), enter a rate by hand with its source, recalculate after product changes;
- the price table: row, product name, part number, minimum order and price per piece, with a search box.

#### Management (مدیریت), approvers and admins

One page for decisions, over 30, 90, 180 or 365 days, with a print button.

![The management page: key numbers, businesses by type, decisions waiting and shortcuts](docs/images/management.png)

- Nine key numbers: the dollar, the list's validity, Borna against wholesale sellers (per piece), Borna against single-unit sellers, the median profit margin, sales in the period, inquiry conversion, the number of businesses found and fresh market prices. Each one opens its own page.
- A card per business type (seller, wholesaler, repair shop, manufacturer) that opens the matching tab of the potential-customers list.
- "Waiting for you": matches and prices to review, a held rate, a list waiting for approval, advisor suggestions, broken sources and sales follow-ups, each with an open button.
- Shortcuts to the price list, the latest weekly report, the advisor, the businesses Excel, the market section and rate history.
- The like-for-like analysis: how many products are cheaper, about the same or dearer than the market on each side, product by product, with charts, plus breakdowns by brand, site and seller.
- Charts and alerts: competitors who cut prices by more than 10%, Borna and competitor trends side by side, the dollar against the list rate, monthly sales (cash and cheque), reasons for lost sales, sales and businesses by city, and the margin of each product.

![The like-for-like analysis: Borna's price against the wholesale median, product by product](docs/images/management_analysis.png)

#### Price list (لیست قیمت)

The official list laid out for one A4 page, with Borna's letterhead, date, validity, the dollar it is based on, and each product's price in rial and toman. Print and Excel buttons. An expired list shows a red "this list is no longer valid" box.

#### Rate history (تاریخچه نرخ)

A chart of accepted rates with the base rate as a line, and a table of every fetch: time, source, rate, status (accepted, waiting, rejected, stale or error) and who decided.

#### Publications (انتشارها)

Every list ever published, with number, kind (base, normal, approved or automatic), date, dollar, validity and publisher. Any old list opens and prints exactly as it went out. Clicking a product shows its official price across all lists, with a chart.

#### Market and competitors (بازار و رقبا)

Thirteen sub-tabs, described [below](#market-and-competitors-the-thirteen-sub-tabs).

#### Sales (فروش)

Eight sub-tabs, described [below](#sales-the-eight-sub-tabs).

#### Products (کالاها), admin

The 27 products with code, list name, part number, base price, $\alpha$ and minimum order. "Update from Excel" previews every change and error before applying it. A removed product is deactivated, never deleted.

#### Users (کاربران), admin

Create users with a role, change roles, deactivate accounts and see last logins. The last admin cannot be deactivated.

#### Events (رویدادها), admin

The log of everything: who did what and when (accepting a rate, publishing, approving a match, downloading an Excel file and so on), filterable by action and user.

#### Settings (تنظیمات), admin

A read-only view of the pricing rules, the rate source, the guards and working hours. They are changed in `settings.ini`.

### Market and competitors: the thirteen sub-tabs

![The market overview: Borna against the wholesale and single-unit medians](docs/images/market_overview.png)

| Sub-tab | What it does |
|---|---|
| Overview (نمای کلی) | Counts of sources, listings, matches and pending work, then the like-for-like comparison: for each side (wholesale and single unit) how many products are cheaper, similar or dearer, a gap chart, and a table with Borna's official per-piece price next to the competitor median, lowest price and gap on each side. |
| Listings (آگهی‌ها) | Every competitor listing, filterable by source and status. A listing's page shows what was read from its title, whether it is genuine or aftermarket, its matches and its price history with evidence. |
| Add competitor price (ثبت قیمت رقیب) | For a price heard on the phone, seen in a shop, on an invoice or in a messenger channel, with a file or a written source. Pack size and seller are recorded. Entries from sales wait for approval. |
| Upload a page (بارگذاری صفحه) | A page saved with Ctrl+S from Torob, Emalls, Digikala, Basalam or Divar. The platform pulls prices, sellers and repair shops out of it. This is the way in when a site blocks automatic reading. |
| Read a price list (خواندن لیست قیمت) | Paste the text of a competitor's price list from a channel. The platform suggests the rows and the matching Borna product, and saves the rows you pick. |
| Match review (تأیید تطبیق) | Each listing next to the Borna product it probably is, with matching details in green and differences in red. Approve, reject, or approve all strong matches in one go. Strong matches with a single candidate are approved automatically. |
| Price review (بررسی قیمت‌ها) | Quarantined prices and sales entries, with the reason and the evidence: "the price is right" or reject. |
| Sources (منبع‌ها) | All sources with their health. A source's page sets how it is read, tests the reader on the last saved page, records permission to monitor, runs it now and lists its saved pages. Admins create new sources. |
| Agents (ایجنت‌ها) | The agent centre: a card per agent, a run log, run-now buttons and a switch that pauses all automatic runs. |
| Site check (بررسی سایت‌ها), admin | Reads one test search ("oxygen sensor") from every market site, the map service and the rate source, and reports for each: working, opened but empty, blocked, unreachable or error, with samples and timing. |
| Weekly report (گزارش هفتگی) | Weekly reports with an Excel file: highlights, each product's position on both sides, how competitors moved during the week and new businesses. Approvers can build one on demand. |
| Ad copy (متن آگهی), approvers | Drafts a title and description for Borna's own listings on Basalam, Digikala or a website, only from recorded specifications, and always says the part is an aftermarket design made by Borna Sensor. |
| AI (هوش مصنوعی), admin | Status and usage of the optional language model, recent requests and a connection test. |

**Each product has its own market page**: the comparison table with Borna's official per-piece price at the top and the wholesale and single-unit statistics below it, two lane charts (Borna's list price against the wholesale median and against the single-unit median, over time), and the price history of every matched listing. A pack listing is marked "pack of N, counted as wholesale".

![A product's market page: the per-piece comparison and the two lane charts](docs/images/product_market.png)

![A weekly market report: highlights in plain words, then the full tables and an Excel file](docs/images/weekly_report.png)

### Sales: the eight sub-tabs

![The sales dashboard: inquiries, conversion, follow-ups for today and reasons for lost sales](docs/images/sales_dashboard.png)

| Sub-tab | What it does |
|---|---|
| Sales dashboard (داشبورد فروش) | Open inquiries, won and lost sales, conversion, time to close, cash against cheque sales, the price gap with competitors, today's follow-ups, reasons for lost sales, sales by product and by customer. |
| Inquiries (استعلام‌ها) | Every customer request for a product. An empty price means the official list price, and an empty follow-up date means two days later. Close it as sold (an order is created too) or lost, with a reason and the competitor's price. That price goes to the market section for review. |
| Customers (مشتری‌ها) | A file per customer: type, city, phone, total purchases, usual time between orders, inquiries and orders. Customers who are due to reorder are pointed out. |
| Orders (سفارش‌ها) | Orders with several lines. Approvers can import orders from the Excel template with a preview first. The same customer's invoice number is never recorded twice. |
| Cheque sale (فروش چکی) | Cash against a post-dated cheque: the smallest acceptable cheque, the amount that keeps the dollar value, and what an offered cheque is worth today. |
| Customer reply (جواب مشتری) | Paste a customer's question. The platform suggests the right product, its official price and a draft answer. A person sends the message. |
| Potential customers (مشتریان احتمالی) | The businesses directory, with a tab and colour per type (seller, wholesaler, repair shop, manufacturer), filters for status, city and phone, and search. Add a phone or a note, mark a business as "became a customer", download the Excel file or load phone numbers back from it. |
| Pricing advisor (مشاور قیمت), approvers | Suggested base-price changes with the reason and an Excel file, and the dollar cost of each product, which feeds the price floor and the margin numbers. |

![Potential customers: every seller, wholesaler and repair shop the agents found, ready to call](docs/images/leads.png)

## Getting started

### What you need

- Python 3.10 or newer.
- The products workbook `data/Borna_Step2-3_Products_Pricing.xlsx`. It is company data and is not in this repository; put it in `data/` before the first start.
- For the market scout: Microsoft Edge or Google Chrome on the same machine. Without one, Digikala and Basalam are skipped and everything else still works.

### Windows

1. Install Python and tick "Add python.exe to PATH".
2. Run `install_windows.bat`. It creates a virtual environment from the offline packages in `wheels/`, asks for an admin password and sets everything up.
3. Run `start_windows.bat` and open `http://127.0.0.1:8080`. Colleagues use the machine's network address instead.

### Ubuntu server

```bash
sudo apt install -y python3-venv unzip
sudo useradd --system --create-home --home-dir /opt/borna_pricing borna
sudo chmod 755 /opt/borna_pricing
# copy the project into /opt/borna_pricing, then:
sudo chown -R borna:borna /opt/borna_pricing
cd /opt/borna_pricing
sudo -u borna bash install_ubuntu.sh
sudo chmod 750 data
sudo -u borna .venv/bin/python -m borna_pricing test-api
```

For the market scout, install Google Chrome and point the platform at it:

```bash
wget -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y /tmp/google-chrome.deb
sudo -u borna sed -i 's/^channel = msedge/channel = chrome/' settings.ini
sudo -u borna .venv/bin/python -m borna_pricing check-sites
```

Then run it as a service, so it starts with the server and comes back after a crash:

```bash
sudo cp borna-pricing.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now borna-pricing
systemctl status borna-pricing --no-pager
```

It listens on port 8080. Keep it inside the office network; if it must face the internet, put it behind HTTPS (the Persian guide has an nginx example).

### Useful commands

All of these run as `python -m borna_pricing <command>` from the project folder (on the server: `sudo -u borna .venv/bin/python -m borna_pricing <command>`).

| Command | What it does |
|---|---|
| `init` | First setup: settings, database, products and the base list. |
| `serve` | Starts the web app, the rate scheduler and the agents. |
| `test-api` | Tests the dollar-rate source without saving anything. |
| `fetch-now` | Fetches the rate now and recalculates. |
| `set-rate` | Records a manual rate (toman). |
| `status` | A short summary of the platform's state. |
| `create-user`, `set-password` | Manage users from the command line. |
| `import-products` | Re-imports products from the workbook. |
| `backup` | Backs up the database. |
| `scout` | Runs the market scout now. |
| `check-sites` | Checks every market site, the map and the rate source, and writes the result to `data/site_check_*.txt`. |

## Tests and quality

- **303 automated tests**; 302 pass and one is skipped because it needs a real browser (it passes when one is provided). They cover the formula against the original workbook, rate checks, publishing and approval rules, roles and CSRF, Excel output and layout, matching, page reading for every site, network guards, quarantine and outliers, the weekly report, database upgrades, the sales desk, the helpers and the management page.
- **Installed for real**: the package was installed offline on Python 3.10 and the suite was run on that installation; database upgrades were tested on a live copy, with every page opening afterwards.
- **Layout checks**: every Excel file is checked for gridlines, borders and text spilling out of its cell, and the pages were crawled at desktop and phone widths to make sure nothing overflows or overlaps.

## Honest limits

- **Wholesale competitor prices are scarce.** The sites the scout reads mostly show single-unit prices. The wholesale side fills up from prices sales enters with the "wholesale" level, pack listings and pasted price lists.
- **Matches need people.** Strong matches are approved automatically, but the rest only count once someone reviews them.
- **Sites can say no, and sites change.** Basalam blocks automated reading and Torob blocks after a burst of requests; the platform respects both, and uploading a saved page is the way around it. When a site changes its page layout, its reader has to be updated; the site check shows this straight away. Emalls changed its pages recently and its reader needs that update.
- **The map service can be down.** OpenStreetMap's Overpass server sometimes answers 502. The scout tries again on its next run.
- **Busy days, many lists.** Because every change is published, a volatile day can produce several lists. Sales should always take the current list from the platform.

## What is not in this repository

To keep company data private, none of the following is included:

- the database and everything the platform collected: competitor listings, evidence pages, customers, businesses, orders and backups;
- `settings.ini` (only `settings.example.ini` is here) and the secret key;
- the products workbook `data/Borna_Step2-3_Products_Pricing.xlsx`. It is needed by `init` and by the test suite, so put your copy in `data/` before running either.
