from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Fields the user types on the invoice. They are frozen once the invoice is posted,
# because the journal entries we create are based on them.
RETENTION_INPUT_FIELDS = {
    'retention_percent', 'retention_handover_date', 'advance_payment_id',
    'advance_recovery_type', 'advance_recovery_percent', 'advance_recovery_fixed',
}


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ------------------------------------------------------------------
    # Fields typed by the user on the customer invoice
    # ------------------------------------------------------------------
    retention_percent = fields.Float(
        string='Retention %', copy=True,
        default=lambda self: self.env.company.retention_default_percent)
    retention_handover_date = fields.Date(
        string='Expected Handover Date', copy=False,
        help="Due date of the retention. The client is not asked to pay it before this date.")
    advance_payment_id = fields.Many2one(
        'account.payment', string='Advance Payment', copy=False, check_company=True,
        domain="[('is_advance', '=', True), ('payment_type', '=', 'inbound'),"
               " ('state', 'in', ('in_process', 'paid')),"
               " ('partner_id.commercial_partner_id', '=', commercial_partner_id)]")
    advance_recovery_type = fields.Selection(
        [('percent', 'Percentage of work done'), ('fixed', 'Fixed amount')],
        string='Recovery Type', default='percent', copy=True)
    advance_recovery_percent = fields.Float(string='Recovery %', copy=True)
    advance_recovery_fixed = fields.Monetary(string='Recovery Amount', currency_field='currency_id', copy=True)

    # ------------------------------------------------------------------
    # Computed amounts shown on the invoice
    # ------------------------------------------------------------------
    retention_amount = fields.Monetary(
        string='Retention', currency_field='currency_id',
        compute='_compute_retention_amounts', store=True)
    advance_recovery_amount = fields.Monetary(
        string='Advance Recovery', currency_field='currency_id',
        compute='_compute_retention_amounts', store=True)
    amount_to_pay_now = fields.Monetary(
        string='Amount to Pay Now', currency_field='currency_id',
        compute='_compute_retention_amounts', store=True)
    advance_remaining = fields.Monetary(
        string='Advance Left', currency_field='currency_id', compute='_compute_advance_remaining',
        help="What is still available on the selected advance payment, in the invoice currency.")
    retention_balance = fields.Monetary(
        string='Retention Still Held', currency_field='currency_id', compute='_compute_retention_balance')
    retention_released = fields.Monetary(
        string='Retention Released', currency_field='currency_id', compute='_compute_retention_balance',
        help="Part of the retention already released to the client (credit notes not included).")
    retention_sale_order_id = fields.Many2one(
        'sale.order', string='Project', compute='_compute_retention_sale_order', store=True,
        help="The sales order the invoice comes from. Credit notes take the one of the invoice they reverse.")

    # ------------------------------------------------------------------
    # Link between a document and the journal entries we create for it
    # ------------------------------------------------------------------
    retention_entry_type = fields.Selection(
        [('adjust', 'Retention / Advance Adjustment'),
         ('cn_adjust', 'Credit Note Adjustment'),
         ('release', 'Retention Release')],
        string='Retention Entry Type', copy=False, readonly=True)
    retention_source_id = fields.Many2one(
        'account.move', string='Retention Source Document', copy=False, readonly=True, index='btree_not_null')
    retention_entry_ids = fields.One2many('account.move', 'retention_source_id', string='Retention Entries')
    retention_entry_retention = fields.Monetary(
        string='Retention in Entry', currency_field='currency_id', copy=False, readonly=True)
    retention_entry_advance = fields.Monetary(
        string='Advance in Entry', currency_field='currency_id', copy=False, readonly=True)

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('move_type', 'amount_untaxed', 'amount_total', 'currency_id', 'retention_percent',
                 'advance_payment_id', 'advance_recovery_type', 'advance_recovery_percent',
                 'advance_recovery_fixed')
    def _compute_retention_amounts(self):
        for move in self:
            retention = recovery = 0.0
            if move.move_type == 'out_invoice':
                currency = move.currency_id or move.company_id.currency_id
                # Retention and recovery are a share of the work done, i.e. the untaxed amount.
                retention = currency.round(move.amount_untaxed * move.retention_percent / 100.0)
                if move.advance_payment_id:
                    if move.advance_recovery_type == 'fixed':
                        recovery = move.advance_recovery_fixed
                    else:
                        recovery = currency.round(move.amount_untaxed * move.advance_recovery_percent / 100.0)
            move.retention_amount = retention
            move.advance_recovery_amount = recovery
            move.amount_to_pay_now = move.amount_total - retention - recovery

    @api.depends('advance_payment_id', 'currency_id', 'advance_payment_id.move_id.line_ids.amount_residual_currency')
    def _compute_advance_remaining(self):
        for move in self:
            if move.advance_payment_id and move.currency_id:
                move.advance_remaining = move.advance_payment_id._get_advance_remaining(
                    move.currency_id, move.date or fields.Date.context_today(move))
            else:
                move.advance_remaining = 0.0

    def _compute_retention_balance(self):
        for move in self:
            line = move._get_retention_line() if move.move_type == 'out_invoice' else False
            if line:
                returned = sum(move._get_all_live_cn_entries().mapped('retention_entry_retention'))
                move.retention_balance = line.amount_residual_currency
                move.retention_released = line.amount_currency - line.amount_residual_currency - returned
            else:
                move.retention_balance = move.retention_released = 0.0

    @api.depends('invoice_line_ids.sale_line_ids.order_id', 'reversed_entry_id')
    def _compute_retention_sale_order(self):
        for move in self:
            order = move.invoice_line_ids.sale_line_ids.order_id[:1]
            if not order and move.reversed_entry_id:
                order = move.reversed_entry_id.retention_sale_order_id
            move.retention_sale_order_id = order

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('retention_percent', 'advance_recovery_percent', 'advance_recovery_fixed',
                    'amount_to_pay_now', 'move_type')
    def _check_retention_values(self):
        for move in self:
            if move.move_type != 'out_invoice':
                continue
            if not 0.0 <= move.retention_percent <= 100.0 or not 0.0 <= move.advance_recovery_percent <= 100.0:
                raise ValidationError(_("Percentages must be between 0 and 100."))
            if move.advance_recovery_fixed < 0.0:
                raise ValidationError(_("The advance recovery amount cannot be negative."))
            # A draft without lines yet cannot be judged: this is checked again when posting.
            if move.amount_total and move.currency_id.compare_amounts(move.amount_to_pay_now, 0.0) < 0:
                raise ValidationError(_(
                    "Retention and advance recovery (%(deducted)s) are more than the invoice total (%(total)s).",
                    deducted=move.retention_amount + move.advance_recovery_amount, total=move.amount_total))

    def write(self, vals):
        if RETENTION_INPUT_FIELDS & vals.keys() and any(move.state == 'posted' for move in self):
            raise UserError(_("Retention and advance fields cannot be changed on a posted invoice. "
                              "Reset it to draft first."))
        return super().write(vals)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_live_retention_entries(self, entry_type=None):
        """Posted entries created for this document that have not been reversed."""
        self.ensure_one()
        return self.retention_entry_ids.filtered(
            lambda entry: entry.state == 'posted'
            and not entry.reversal_move_ids.filtered(lambda r: r.state == 'posted')
            and (not entry_type or entry.retention_entry_type == entry_type))

    def _get_retention_line(self):
        """Open retention item (the debit line in the retention account) of this invoice."""
        self.ensure_one()
        entry = self._get_live_retention_entries('adjust')[:1]
        return entry.line_ids.filtered(lambda l: l.retention_origin_id and l.debit > 0)[:1]

    def _retention_to_company(self, amount):
        """Convert an amount in the invoice currency with the rate the invoice itself used."""
        self.ensure_one()
        ratio = abs(self.amount_total_signed) / self.amount_total if self.amount_total else 1.0
        return self.company_currency_id.round(amount * ratio)

    def _retention_line_vals(self, name, account, amount_currency, balance, **extra):
        """Values of one journal item. `amount_currency` and `balance` are both given
        (debit positive) so the entry is balanced exactly, whatever the currency."""
        self.ensure_one()
        return Command.create({
            'name': name,
            'account_id': account.id,
            'partner_id': self.commercial_partner_id.id,
            'currency_id': self.currency_id.id,
            'amount_currency': amount_currency,
            'debit': balance if balance > 0 else 0.0,
            'credit': -balance if balance < 0 else 0.0,
            **extra,
        })

    def _retention_create_entry(self, entry_type, date, ref, line_commands, retention=0.0, advance=0.0, source=None):
        self.ensure_one()
        entry = self.env['account.move'].with_company(self.company_id).create({
            'move_type': 'entry',
            'journal_id': self.company_id._retention_get_journal().id,
            'date': date,
            'ref': ref,
            'partner_id': self.commercial_partner_id.id,
            'currency_id': self.currency_id.id,
            'company_id': self.company_id.id,
            'retention_entry_type': entry_type,
            'retention_source_id': (source or self).id,
            'retention_entry_retention': retention,
            'retention_entry_advance': advance,
            'line_ids': line_commands,
        })
        entry.action_post()
        return entry

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------
    def _check_retention_ready_to_post(self):
        """Every check is done before the invoice gets posted, so a wrong input leaves it in draft."""
        self.ensure_one()
        if self.currency_id.compare_amounts(self.amount_to_pay_now, 0.0) < 0:
            raise UserError(_(
                "Retention and advance recovery (%(deducted)s) are more than the invoice total (%(total)s).",
                deducted=self.retention_amount + self.advance_recovery_amount, total=self.amount_total))
        if self.retention_amount and not self.company_id.retention_account_id:
            raise UserError(_("Select the Retention Receivable account in the accounting settings first."))
        if self.retention_amount and not self.retention_handover_date:
            raise UserError(_("Enter the expected handover date: it is the due date of the retention."))
        if not self.advance_recovery_amount:
            return
        payment = self.advance_payment_id
        if payment.company_id != self.company_id \
                or payment.partner_id.commercial_partner_id != self.commercial_partner_id:
            raise UserError(_("The advance payment must belong to the same company and customer as the invoice."))
        remaining = payment._get_advance_remaining(self.currency_id, self.date or fields.Date.context_today(self))
        if self.currency_id.compare_amounts(self.advance_recovery_amount, remaining) > 0:
            raise UserError(_(
                "You cannot recover %(amount)s: only %(left)s is left on the advance %(payment)s.",
                amount=self.currency_id.format(self.advance_recovery_amount),
                left=self.currency_id.format(remaining),
                payment=payment.display_name))

    def _post(self, soft=True):
        to_adjust = self.filtered(
            lambda m: m.move_type == 'out_invoice' and (m.retention_amount or m.advance_recovery_amount))
        for invoice in to_adjust.filtered(lambda m: m.state == 'draft'):
            invoice._check_retention_ready_to_post()
        posted = super()._post(soft=soft)
        for invoice in posted.filtered(lambda m: m in to_adjust):
            # Re-posting after a reset to draft: the old entry was reversed, a new one is needed.
            if not invoice._get_live_retention_entries('adjust'):
                invoice._create_retention_adjustment_entry()
        for credit_note in posted.filtered(lambda m: m.move_type == 'out_refund' and m.reversed_entry_id):
            if not credit_note._get_live_retention_entries('cn_adjust'):
                credit_note._create_credit_note_adjustment_entry()
        return posted

    def _create_retention_adjustment_entry(self):
        """Move the retention and the advance recovery out of the normal receivable.

            Dr Retention Receivable     retention   (due at handover)
            Dr Receivable               recovery    (matched with the advance payment)
                Cr Receivable                       retention + recovery (matched with the invoice)

        The invoice itself is not touched: its receivable is simply reduced by the matching.
        """
        self.ensure_one()
        retention, recovery = self.retention_amount, self.advance_recovery_amount
        receivable_lines = self.line_ids.filtered(lambda l: l.display_type == 'payment_term')
        receivable_account = receivable_lines[:1].account_id
        retention_balance = self._retention_to_company(retention)
        recovery_balance = self._retention_to_company(recovery)
        name = _("%s - retention / advance", self.name)

        commands = []
        if retention:
            commands.append(self._retention_line_vals(
                _("Retention %s", self.name), self.company_id.retention_account_id, retention, retention_balance,
                date_maturity=self.retention_handover_date, retention_origin_id=self.id))
        if recovery:
            commands.append(self._retention_line_vals(
                _("Advance recovery %s", self.name), receivable_account, recovery, recovery_balance,
                date_maturity=self.invoice_date_due, retention_advance_payment_id=self.advance_payment_id.id))
        commands.append(self._retention_line_vals(
            name, receivable_account, -(retention + recovery), -(retention_balance + recovery_balance),
            date_maturity=max(receivable_lines.mapped('date_maturity') or [self.invoice_date_due])))
        entry = self._retention_create_entry('adjust', self.date, name, commands, retention, recovery)

        # Matching: the credit line takes the amount off the invoice, the advance line takes it off the payment.
        credit_line = entry.line_ids.filtered(lambda l: l.credit and not l.retention_origin_id)
        # Latest instalments are reduced first.
        (receivable_lines.sorted('date_maturity', reverse=True) + credit_line).reconcile()
        if recovery:
            advance_line = entry.line_ids.filtered('retention_advance_payment_id')
            (advance_line + self.advance_payment_id._get_advance_line()).reconcile()
        return entry

    def _create_credit_note_adjustment_entry(self):
        """Give back retention and advance in the same proportion as the credit note reverses the invoice.

            Dr Receivable               retention + advance   (matched with the credit note)
                Cr Retention Receivable                retention  (matched with the retention item)
                Cr Receivable                          advance    (matched with the original recovery debit)

        The returned advance replaces part of the original payment/recovery matching.
        This opens the same amount again on the selected advance payment.
        """
        self.ensure_one()
        invoice = self.reversed_entry_id
        base_entry = invoice._get_live_retention_entries('adjust')[:1]
        if invoice.move_type != 'out_invoice' or not base_entry:
            return
        # The credit note and invoice are in the same document currency. Company
        # currency totals may use different rates, so they cannot define the share.
        ratio = min(abs(self.amount_total) / abs(invoice.amount_total), 1.0)
        currency = invoice.currency_id
        earlier = invoice._get_all_live_cn_entries()  # what other credit notes already gave back
        retention_line = invoice._get_retention_line()

        retention = min(
            currency.round(invoice.retention_amount * ratio),
            invoice.retention_amount - sum(earlier.mapped('retention_entry_retention')),
            # If part of the retention was already released it is a normal receivable now: the credit note covers it.
            retention_line.amount_residual_currency if retention_line else 0.0)
        advance = min(
            currency.round(invoice.advance_recovery_amount * ratio),
            invoice.advance_recovery_amount - sum(earlier.mapped('retention_entry_advance')))
        retention, advance = max(retention, 0.0), max(advance, 0.0)
        if not retention and not advance:
            return

        receivable_account = base_entry.line_ids.filtered(lambda l: l.credit and not l.retention_origin_id).account_id
        retention_balance = invoice._retention_to_company(retention)
        advance_balance = invoice._retention_to_company(advance)
        maturity = self.invoice_date_due or self.date
        name = _("%s - retention / advance returned", self.name)

        commands = [invoice._retention_line_vals(
            name, receivable_account, retention + advance, retention_balance + advance_balance,
            date_maturity=maturity)]
        if retention:
            commands.append(invoice._retention_line_vals(
                _("Retention returned %s", self.name), self.company_id.retention_account_id,
                -retention, -retention_balance, date_maturity=retention_line.date_maturity,
                retention_origin_id=invoice.id))
        if advance:
            commands.append(invoice._retention_line_vals(
                _("Advance given back %s", self.name), receivable_account, -advance, -advance_balance,
                date_maturity=maturity, retention_advance_payment_id=invoice.advance_payment_id.id))
        # The entry belongs to the credit note: it is reversed together with it.
        entry = invoice._retention_create_entry(
            'cn_adjust', self.date, name, commands, retention, advance, source=self)

        # Odoo has already matched the credit note with the invoice when posting it. Undo that match, let our
        # entry take its part first (the retention and advance stay what they are), then match the rest again.
        credit_lines = self.line_ids.filtered(lambda l: l.display_type == 'payment_term')
        invoice_matches = credit_lines.matched_debit_ids.filtered(lambda p: p.debit_move_id.move_id == invoice)
        was_matched = bool(invoice_matches)
        invoice_matches.unlink()

        (credit_lines + entry.line_ids.filtered(lambda l: l.debit)).reconcile()
        if retention:
            (retention_line + entry.line_ids.filtered('retention_origin_id')).reconcile()
        if advance:
            invoice._match_returned_advance(base_entry, entry)
        if was_matched:
            invoice_lines = invoice.line_ids.filtered(lambda l: l.display_type == 'payment_term' and not l.reconciled)
            (invoice_lines + credit_lines.filtered(lambda l: not l.reconciled)).reconcile()
        return entry

    def _match_returned_advance(self, base_entry, return_entry):
        """Move this credit note's returned advance back onto the original payment."""
        self.ensure_one()
        payment_line = self.advance_payment_id._get_advance_line()
        recovery_line = base_entry.line_ids.filtered(
            lambda line: line.debit and line.retention_advance_payment_id == self.advance_payment_id)[:1]
        return_line = return_entry.line_ids.filtered(
            lambda line: line.credit and line.retention_advance_payment_id == self.advance_payment_id)[:1]
        # Remove only the matching for this invoice's recovery; other invoices may
        # also have used the payment and must remain matched.
        recovery_line.matched_credit_ids.filtered(
            lambda partial: partial.credit_move_id in payment_line).unlink()
        (recovery_line + return_line).reconcile()
        if not self.currency_id.is_zero(recovery_line.amount_residual_currency):
            (recovery_line + payment_line).reconcile()

    def _rematch_advance_after_credit_reset(self):
        """A reset reverses the credit note's return; consume that amount again."""
        self.ensure_one()
        base_entry = self._get_live_retention_entries('adjust')[:1]
        if not base_entry or not self.advance_payment_id:
            return
        recovery_line = base_entry.line_ids.filtered(
            lambda line: line.debit and line.retention_advance_payment_id == self.advance_payment_id)[:1]
        if recovery_line and not self.currency_id.is_zero(recovery_line.amount_residual_currency):
            (recovery_line + self.advance_payment_id._get_advance_line()).reconcile()

    def _get_all_live_cn_entries(self):
        """Live credit-note adjustments of this invoice (their source is the credit note, not the invoice)."""
        self.ensure_one()
        credit_notes = self.env['account.move'].search([('reversed_entry_id', '=', self.id)])
        return credit_notes.retention_entry_ids.filtered(
            lambda e: e.retention_entry_type == 'cn_adjust' and e.state == 'posted'
            and not e.reversal_move_ids.filtered(lambda r: r.state == 'posted'))

    # ------------------------------------------------------------------
    # Reset to draft: reverse our entries, never delete them
    # ------------------------------------------------------------------
    def button_draft(self):
        for move in self:
            move._check_retention_can_reset()
        res = super().button_draft()
        for move in self:
            move._reverse_retention_entries()
            if move.move_type == 'out_refund' and move.reversed_entry_id:
                move.reversed_entry_id._rematch_advance_after_credit_reset()
        return res

    def _check_retention_can_reset(self):
        self.ensure_one()
        if self.move_type == 'out_invoice' and self.state == 'posted':
            if self._get_all_live_cn_entries():
                raise UserError(_("Reset the credit notes of this invoice to draft first."))
            entry = self._get_live_retention_entries('adjust')[:1]
            line = self._get_retention_line()
            if entry and line and not self.currency_id.is_zero(line.amount_residual_currency - line.amount_currency):
                raise UserError(_("Part of the retention was already released. "
                                  "Reverse the release entry before resetting the invoice to draft."))

    def _reverse_retention_entries(self):
        self.ensure_one()
        for entry in self.retention_entry_ids.filtered(
                lambda e: e.state == 'posted' and not e.reversal_move_ids.filtered(lambda r: r.state == 'posted')):
            # Reverse on the original accounting date so an as-of report cannot
            # count both the old and the re-posted adjustment in that period.
            # Posting the reversal will refuse a locked period.
            date = entry.date
            # cancel=True removes the matchings (invoice, payment) and matches the reversal with the entry.
            entry._reverse_moves([{'date': date, 'ref': _("Reversal of %s", entry.ref or entry.name)}], cancel=True)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def action_open_retention_entries(self):
        self.ensure_one()
        entries = self.retention_entry_ids | self._get_all_live_cn_entries()
        entries |= entries.reversal_move_ids
        return {
            'type': 'ir.actions.act_window',
            'name': _("Retention Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', entries.ids)],
        }
