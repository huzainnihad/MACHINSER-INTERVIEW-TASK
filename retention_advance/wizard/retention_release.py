from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError


class RetentionReleaseWizard(models.TransientModel):
    _name = 'retention.release.wizard'
    _description = 'Release Retention'

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    partner_id = fields.Many2one(
        'res.partner', string='Customer', required=True,
        domain="[('parent_id', '=', False)]")
    date = fields.Date(string='Release Date', required=True, default=fields.Date.context_today)
    release_mode = fields.Selection(
        [('full', 'Full'), ('partial', 'Partial')], string='Release', default='full', required=True)
    amount = fields.Monetary(
        string='Amount to Release', currency_field='currency_id',
        compute='_compute_amount', store=True, readonly=False)
    currency_id = fields.Many2one('res.currency', compute='_compute_currency_id')
    total_selected = fields.Monetary(currency_field='currency_id', compute='_compute_total_selected')
    mixed_currencies = fields.Boolean(compute='_compute_currency_id')
    line_ids = fields.One2many(
        'retention.release.wizard.line', 'wizard_id', string='Retention Items',
        compute='_compute_line_ids', store=True, readonly=False)

    @api.depends('partner_id', 'company_id')
    def _compute_line_ids(self):
        for wizard in self:
            commands = [Command.clear()]
            if wizard.partner_id and wizard.company_id.retention_account_id:
                # Open debit items of the retention account created by our adjustment entries.
                items = self.env['account.move.line'].search([
                    ('company_id', '=', wizard.company_id.id),
                    ('account_id', '=', wizard.company_id.retention_account_id.id),
                    ('partner_id', '=', wizard.partner_id.id),
                    ('parent_state', '=', 'posted'),
                    ('retention_origin_id', '!=', False),
                    ('debit', '>', 0),
                    ('reconciled', '=', False),
                ], order='date_maturity, id')
                # Items in different currencies are released separately: only the first currency is ticked.
                commands += [
                    Command.create({'move_line_id': item.id, 'selected': item.currency_id == items[:1].currency_id})
                    for item in items]
            wizard.line_ids = commands

    @api.depends('line_ids.currency_id', 'line_ids.selected')
    def _compute_currency_id(self):
        for wizard in self:
            # The currency of the release is the one of the items ticked, not of the first one in the list.
            selected = wizard.line_ids.filtered('selected')
            wizard.mixed_currencies = len(selected.currency_id) > 1
            wizard.currency_id = (
                selected.currency_id[:1] or wizard.line_ids.currency_id[:1] or wizard.company_id.currency_id)

    @api.depends('line_ids.selected', 'line_ids.residual')
    def _compute_total_selected(self):
        for wizard in self:
            wizard.total_selected = sum(wizard.line_ids.filtered('selected').mapped('residual'))

    @api.depends('release_mode', 'total_selected')
    def _compute_amount(self):
        for wizard in self:
            if wizard.release_mode == 'full' or not wizard.amount:
                wizard.amount = wizard.total_selected

    def _allocate(self):
        """Share the amount between the selected items, earliest handover first."""
        self.ensure_one()
        left = self.amount
        allocation = {}
        for line in self.line_ids.filtered('selected').sorted(lambda l: (l.date_maturity or fields.Date.today(), l.id)):
            part = min(left, line.residual) if left > 0 else 0.0
            allocation[line] = part
            left -= part
        return allocation

    def action_release(self):
        self.ensure_one()
        selected = self.line_ids.filtered('selected')
        if not selected:
            raise UserError(_("Select at least one invoice to release."))
        if len(selected.currency_id) > 1:
            raise UserError(_("Release invoices in different currencies separately."))
        currency, company = self.currency_id, self.company_id
        if currency.compare_amounts(self.amount, 0.0) <= 0:
            raise UserError(_("The amount to release must be positive."))
        if currency.compare_amounts(self.amount, self.total_selected) > 0:
            raise UserError(_("You cannot release more than the retention held on the selected invoices (%s).",
                              currency.format(self.total_selected)))
        locks = company.sudo()._get_lock_date_violations(
            self.date, fiscalyear=True, sale=False, purchase=False, tax=False, hard=True)
        if locks:
            raise UserError(_("You cannot release retention on %(date)s: the period is locked (%(locks)s).",
                              date=self.date, locks=self.env['res.company']._format_lock_dates(locks)))
        allocation = {line: part for line, part in self._allocate().items() if part}

        # Credit side: one line per invoice on the retention account. Debit side: one line on the receivable.
        credit_commands, total_balance = [], 0.0
        for line, part in allocation.items():
            balance = company.currency_id.round(currency._convert(part, company.currency_id, company, self.date))
            total_balance += balance
            credit_commands.append(Command.create({
                'name': _("Retention release %s", line.invoice_id.name),
                'account_id': line.move_line_id.account_id.id,
                'partner_id': self.partner_id.id,
                'currency_id': currency.id,
                'amount_currency': -part,
                'credit': balance,
                'date_maturity': line.date_maturity,
                'retention_origin_id': line.invoice_id.id,
            }))
        receivable = self.partner_id.with_company(company).property_account_receivable_id
        debit_command = Command.create({
            'name': _("Retention released"),
            'account_id': receivable.id,
            'partner_id': self.partner_id.id,
            'currency_id': currency.id,
            'amount_currency': sum(allocation.values()),
            'debit': total_balance,
            'date_maturity': self.date,  # now due like any other invoice
        })
        entry = self.env['account.move'].with_company(company).create({
            'move_type': 'entry',
            'journal_id': company._retention_get_journal().id,
            'date': self.date,
            'ref': _("Retention release - %s", self.partner_id.name),
            'partner_id': self.partner_id.id,
            'currency_id': currency.id,
            'company_id': company.id,
            'retention_entry_type': 'release',
            'line_ids': [debit_command] + credit_commands,
        })
        entry.action_post()  # lock dates are checked here

        credit_lines = entry.line_ids.filtered('credit').sorted('id')
        for credit_line, line in zip(credit_lines, allocation):
            (line.move_line_id + credit_line).reconcile()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': entry.id,
            'view_mode': 'form',
        }


class RetentionReleaseWizardLine(models.TransientModel):
    _name = 'retention.release.wizard.line'
    _description = 'Release Retention Line'
    _order = 'date_maturity, id'

    wizard_id = fields.Many2one('retention.release.wizard', required=True, ondelete='cascade')
    move_line_id = fields.Many2one('account.move.line', string='Retention Item', required=True, readonly=True)
    invoice_id = fields.Many2one(related='move_line_id.retention_origin_id', string='Invoice')
    sale_order_id = fields.Many2one(related='invoice_id.retention_sale_order_id', string='Project')
    date_maturity = fields.Date(related='move_line_id.date_maturity', string='Handover Date')
    currency_id = fields.Many2one(related='move_line_id.currency_id')
    residual = fields.Monetary(
        string='Retention Held', currency_field='currency_id', compute='_compute_residual')
    selected = fields.Boolean(default=True)
    amount_to_release = fields.Monetary(
        currency_field='currency_id', compute='_compute_amount_to_release')

    @api.depends('move_line_id.amount_residual_currency')
    def _compute_residual(self):
        for line in self:
            line.residual = line.move_line_id.amount_residual_currency

    @api.depends('selected', 'residual', 'wizard_id.amount', 'wizard_id.line_ids.selected')
    def _compute_amount_to_release(self):
        for line in self:
            line.amount_to_release = line.wizard_id._allocate().get(line, 0.0) if line.selected else 0.0
