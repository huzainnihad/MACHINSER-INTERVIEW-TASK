import base64
import io
from collections import defaultdict

import xlsxwriter

from odoo import Command, _, api, fields, models

AMOUNT_FIELDS = [
    ('total_invoiced', 'Total Invoiced'),
    ('retention_held', 'Retention Held'),
    ('retention_released', 'Retention Released'),
    ('retention_balance', 'Retention Balance'),
    ('advance_received', 'Advance Received'),
    ('advance_recovered', 'Advance Recovered'),
    ('advance_balance', 'Advance Balance'),
]


class RetentionReportWizard(models.TransientModel):
    _name = 'retention.report.wizard'
    _description = 'Retention and Advance Report'

    as_of_date = fields.Date(string='As at', required=True, default=fields.Date.context_today)
    company_ids = fields.Many2many(
        'res.company', string='Companies', required=True,
        default=lambda self: self.env.companies)
    partner_ids = fields.Many2many('res.partner', string='Customers', domain="[('parent_id', '=', False)]")
    line_ids = fields.One2many('retention.report.line', 'wizard_id', string='Lines')

    @api.depends('as_of_date')
    def _compute_display_name(self):
        for wizard in self:
            wizard.display_name = _("Retention and Advance Report")

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------
    @staticmethod
    def _entry_type(entry):
        """Type of one of our entries; a reversal counts as the entry it reverses."""
        return entry.retention_entry_type or entry.reversed_entry_id.retention_entry_type

    def _compute_rows(self):
        """Everything is read from posted journal items and payments dated up to the chosen date,
        so the report can be run for any past date. Amounts are in each company's currency."""
        self.ensure_one()
        Move, Line, Payment = self.env['account.move'], self.env['account.move.line'], self.env['account.payment']
        rows = defaultdict(lambda: dict.fromkeys(dict(AMOUNT_FIELDS), 0.0))
        companies = self.company_ids
        partner_domain = [('partner_id', 'in', self.partner_ids.ids)] if self.partner_ids else []

        def key(company, partner, order):
            return (company.id, partner.commercial_partner_id.id, order.id)

        # Total invoiced: invoices minus credit notes, VAT included.
        for move in Move.search([
                ('move_type', 'in', ('out_invoice', 'out_refund')), ('state', '=', 'posted'),
                ('date', '<=', self.as_of_date), ('company_id', 'in', companies.ids)]
                + ([('commercial_partner_id', 'in', self.partner_ids.ids)] if self.partner_ids else [])):
            sign = -1 if move.move_type == 'out_refund' else 1
            rows[key(move.company_id, move.partner_id, move.retention_sale_order_id)]['total_invoiced'] += \
                sign * abs(move.amount_total_signed)

        # Retention: every item in our entries. Reversals copy the link, so they cancel the original.
        for line in Line.search([
                ('retention_origin_id', '!=', False), ('parent_state', '=', 'posted'),
                ('date', '<=', self.as_of_date), ('company_id', 'in', companies.ids)] + partner_domain):
            entry_type = self._entry_type(line.move_id)
            invoice = line.retention_origin_id
            row = rows[key(line.company_id, invoice.partner_id, invoice.retention_sale_order_id)]
            if entry_type in ('adjust', 'cn_adjust'):
                row['retention_held'] += line.balance
            elif entry_type == 'release':
                row['retention_released'] -= line.balance

        # Foreign currency: releasing at another rate makes Odoo post an exchange difference on the retention
        # account. It belongs to the release (or to the credit note), so the balance reaches zero as in the ledger.
        retention_accounts = companies.retention_account_id
        for line in Line.search([
                ('account_id', 'in', retention_accounts.ids), ('parent_state', '=', 'posted'),
                ('move_id.exchange_diff_partial_ids', '!=', False),
                ('date', '<=', self.as_of_date), ('company_id', 'in', companies.ids)] + partner_domain):
            sides = line.move_id.exchange_diff_partial_ids[:1]
            sides = sides.debit_move_id | sides.credit_move_id
            origin_line = sides.filtered('retention_origin_id')[:1]
            if not origin_line:
                continue
            kinds = {self._entry_type(side.move_id) for side in sides} - {'adjust'}
            invoice = origin_line.retention_origin_id
            row = rows[key(line.company_id, invoice.partner_id, invoice.retention_sale_order_id)]
            if 'release' in kinds:
                row['retention_released'] -= line.balance
            elif 'cn_adjust' in kinds:
                row['retention_held'] += line.balance

        # Advance: received = advance payments, recovered = our journal items flagged with the payment.
        for payment in Payment.search([
                ('is_advance', '=', True), ('payment_type', '=', 'inbound'),
                ('move_id.state', '=', 'posted'), ('date', '<=', self.as_of_date),
                ('company_id', 'in', companies.ids)] + partner_domain):
            rows[key(payment.company_id, payment.partner_id, payment.advance_sale_order_id)]['advance_received'] += \
                abs(payment.amount_company_currency_signed)
        for line in Line.search([
                ('retention_advance_payment_id', '!=', False), ('parent_state', '=', 'posted'),
                ('date', '<=', self.as_of_date), ('company_id', 'in', companies.ids)] + partner_domain):
            payment = line.retention_advance_payment_id
            rows[key(line.company_id, payment.partner_id, payment.advance_sale_order_id)]['advance_recovered'] += \
                line.balance

        for values in rows.values():
            values['retention_balance'] = values['retention_held'] - values['retention_released']
            values['advance_balance'] = values['advance_received'] - values['advance_recovered']
        return rows

    def action_generate(self):
        self.ensure_one()
        rows = self._compute_rows()
        commands = [Command.clear()]
        for (company_id, partner_id, order_id), values in sorted(rows.items()):
            if not any(values.values()):
                continue
            commands.append(Command.create({
                'company_id': company_id, 'partner_id': partner_id, 'sale_order_id': order_id or False, **values}))
        self.line_ids = commands
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Excel
    # ------------------------------------------------------------------
    def action_export_xlsx(self):
        self.ensure_one()
        self.action_generate()
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet(_("Retention and Advance"))
        bold = workbook.add_format({'bold': True, 'bg_color': '#D9E1F2', 'border': 1, 'text_wrap': True})
        title = workbook.add_format({'bold': True, 'font_size': 14})
        money = workbook.add_format({'num_format': '#,##0.00'})
        total_fmt = workbook.add_format({'num_format': '#,##0.00', 'bold': True, 'top': 1})

        sheet.write(0, 0, _("Retention and Advance Report"), title)
        sheet.write(1, 0, _("As at"))
        sheet.write(1, 1, str(self.as_of_date))
        headers = [_("Company"), _("Customer"), _("Project")] + [label for _name, label in AMOUNT_FIELDS]
        for col, header in enumerate(headers):
            sheet.write(3, col, header, bold)
        row = 4
        for line in self.line_ids:
            sheet.write(row, 0, line.company_id.name)
            sheet.write(row, 1, line.partner_id.name)
            sheet.write(row, 2, line.sale_order_id.name or _("No project"))
            for col, (name, _label) in enumerate(AMOUNT_FIELDS, start=3):
                sheet.write_number(row, col, line[name], money)
            row += 1
        if len(self.company_ids) == 1:  # totals only make sense in a single currency
            sheet.write(row, 2, _("Total"), total_fmt)
            for col, (name, _label) in enumerate(AMOUNT_FIELDS, start=3):
                sheet.write_number(row, col, sum(self.line_ids.mapped(name)), total_fmt)
        sheet.set_column(0, 2, 28)
        sheet.set_column(3, 9, 18)
        workbook.close()

        attachment = self.env['ir.attachment'].create({
            'name': f"retention_advance_report_{self.as_of_date}.xlsx",
            'datas': base64.b64encode(output.getvalue()),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }


class RetentionReportLine(models.TransientModel):
    _name = 'retention.report.line'
    _description = 'Retention and Advance Report Line'
    _order = 'company_id, partner_id, sale_order_id'

    wizard_id = fields.Many2one('retention.report.wizard', required=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one('res.partner', string='Customer', readonly=True)
    sale_order_id = fields.Many2one('sale.order', string='Project', readonly=True)
    total_invoiced = fields.Monetary(currency_field='currency_id', readonly=True)
    retention_held = fields.Monetary(currency_field='currency_id', readonly=True)
    retention_released = fields.Monetary(currency_field='currency_id', readonly=True)
    retention_balance = fields.Monetary(currency_field='currency_id', readonly=True)
    advance_received = fields.Monetary(currency_field='currency_id', readonly=True)
    advance_recovered = fields.Monetary(currency_field='currency_id', readonly=True)
    advance_balance = fields.Monetary(currency_field='currency_id', readonly=True)
