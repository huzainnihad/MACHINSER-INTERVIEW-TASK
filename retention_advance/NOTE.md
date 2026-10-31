# Retention and Advance on Invoices - note

Module: `retention_advance` (Odoo 19, depends on `account` and `sale`). No core file is changed.

## Idea in one sentence
The customer invoice is posted by Odoo as usual (full receivable). Right after posting, the module creates **one
extra journal entry** that moves the retention to the Retention Receivable account and matches the advance recovery
with the advance payment. Posted entries are never edited: to undo, the entry is **reversed**.

## Journal entries (example: work 100,000, VAT 15%, retention 10%, recovery 20%)
| # | When | Entry |
|---|------|-------|
| 0 | Advance received (normal payment, flag "Advance Payment") | Dr Bank 200,000 / Cr Receivable 200,000 |
| 1 | Invoice posted (standard) | Dr Receivable 115,000 / Cr Revenue 100,000 / Cr VAT 15,000 |
| 2 | Right after (module) | Dr Retention Receivable 10,000 (due = handover date) <br> Dr Receivable 20,000 (matched with the advance payment) <br> Cr Receivable 30,000 (matched with the invoice) |

Result: invoice residual = 85,000, retention account = 10,000, advance left = 180,000.

| Event | Entry |
|-------|-------|
| Release retention (amount X) | Dr Receivable X (due on release date, a normal due amount) / Cr Retention Receivable X (matched with the retention item) |
| Credit note, proportion p of the invoice | Dr Receivable (p x retention + p x recovery), matched with the credit note <br> Cr Retention Receivable p x retention, matched with the retention item <br> Cr Receivable p x recovery, matched with the original recovery debit. That part of the selected advance payment becomes available again. |
| Reset to draft | The adjustment entry is reversed on its original date (matchings removed first, so the payment gets its advance back). Posting again creates a new entry. A locked original period prevents the reset. |

## Where to find things
Settings > Accounting (account + default %), confirmed sales order > Advance Payment (prefilled draft
payment) and Advance Payments smart button, invoice tab "Retention & Advance", payment form (tick *Advance
Payment*, optional project), Customers > Release Retention (also a button on the customer form),
Reporting > Retention and Advance (Excel export button), printed invoice shows the deductions.

## Assumptions
- Retention % and recovery % apply to the **untaxed** amount (work done). Fixed recovery is in the invoice currency.
- Retention account must be of type *Receivable* (so it can be matched and has due dates), per company.
- The advance is a normal customer payment flagged *Advance Payment*. Its project (sales order) is optional; the
  report groups the advance by the project set on the payment, otherwise under the customer only.
- The sales order button opens a draft customer payment with the customer, company and sales order prefilled. The
  accountant enters the amount, date, currency and journal, then posts it after receiving the funds. The amount is
  not inferred from the invoice recovery percentage.
- All active advances linked to one sales order are capped at its displayed Total (including VAT). Drafts are
  checked against advances already posted; posting locks the order and checks the whole batch again. Payments in
  another currency are converted to the order currency at each payment date. An advance without a project has no
  order-based ceiling.
- Project of an invoice = first sales order of its lines; credit notes use the one of the invoice they reverse.
- Expected handover date is required when retention > 0 (it is the due date of the retention).
- Release: all selected items must be in the same currency; the amount is shared earliest handover date first.
- Credit note: returned retention is capped by what is still held (if part was released it is already a normal
  receivable, and the credit note covers it normally). The returned advance is matched with the original recovery
  line, restoring that amount on the selected advance payment. Resetting the credit note rematches the payment.
- An invoice cannot be reset to draft once part of its retention was released or returned (reverse the release or
  reset the credit note first).
- Release dates in a locked period (fiscal or hard lock) are refused.
- Foreign currency: a credit note's proportion is measured in the invoice currency, and its returned adjustment
  uses the original invoice rate. A release at another rate creates Odoo's usual exchange difference; the report
  adds it to "released" so the balance reaches zero as in the ledger.
- Report amounts are in each company's currency, one row per company / customer / project.

## Not done / limits
- No multi-instalment retention (one retention item per invoice).
- Report is computed in Python from journal items (fine for a project portfolio, not tuned for millions of lines).
- The standard "Paid on" widget of the printed invoice also lists the matching with the adjustment entry.
- Automated tests cover the example, 10 invoices, release (full/partial), credit notes and advance reuse,
  reset/repost and historical reports, USD, two companies, lock dates and Excel. The demonstration video shows
  invoice posting, retention release and the report in the Odoo browser.
