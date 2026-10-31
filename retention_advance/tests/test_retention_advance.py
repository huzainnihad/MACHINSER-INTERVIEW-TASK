from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged('post_install', '-at_install')
class TestRetentionAdvance(AccountTestInvoicingCommon):
    """The example of the brief: project 1,000,000, advance 200,000, retention 10%, recovery 20%,
    first progress invoice of 100,000 + 15% VAT."""
    chart_template = 'sa'
    country_code = 'SA'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls._create_company(name='SA Contractor')
        cls.env.user.company_ids |= cls.company
        cls.env.user.company_id = cls.company
        cls.env.user.group_ids |= cls.env.ref('sales_team.group_sale_manager')
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        cls.setup_company_retention(cls.company)
        cls.vat = cls.env['account.tax'].search([
            ('company_id', '=', cls.company.id), ('type_tax_use', '=', 'sale'), ('amount', '=', 15.0)], limit=1)

        cls.customer = cls.env['res.partner'].create({'name': 'Client A'})
        cls.service = cls.env['product.product'].create({
            'name': 'Progress work', 'type': 'service', 'list_price': 1.0,
            'taxes_id': [Command.set(cls.vat.ids)],
        })

    @classmethod
    def setup_company_retention(cls, company):
        Account = cls.env['account.account'].with_company(company)
        account = Account.create({
            'name': 'Retention Receivable', 'code': '129999',
            'account_type': 'asset_receivable', 'company_ids': [Command.set(company.ids)],
        })
        company.retention_account_id = account
        company.retention_default_percent = 10.0


    # ------------------------------------------------------------------ helpers
    def _sale_order(self, partner=None, amount=1000000.0, company=None):
        order = self.env['sale.order'].with_company(company or self.company).create({
            'partner_id': (partner or self.customer).id,
            'company_id': (company or self.company).id,
            'order_line': [Command.create({
                'product_id': self.service.id, 'product_uom_qty': 1, 'price_unit': amount})],
        })
        order.action_confirm()
        return order

    def _advance(self, order=None, amount=200000.0, currency=None):
        journal = self.env['account.journal'].search(
            [('type', '=', 'bank'), ('company_id', '=', self.company.id)], limit=1)
        payment = self.env['account.payment'].create({
            'payment_type': 'inbound', 'partner_type': 'customer', 'partner_id': self.customer.id,
            'amount': amount, 'journal_id': journal.id, 'date': '2026-01-01',
            'is_advance': True, 'advance_sale_order_id': order.id if order else False,
            **({'currency_id': currency.id} if currency else {}),
        })
        payment.action_post()
        return payment

    def _invoice(self, order, work=100000.0, payment=None, recovery=20.0, date='2026-02-01',
                 handover='2027-02-01', currency=None, post=True, retention=10.0):
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': order.partner_id.id,
            'company_id': order.company_id.id,
            'invoice_date': date,
            'currency_id': (currency or order.company_id.currency_id).id,
            'invoice_line_ids': [Command.create({
                'product_id': self.service.id, 'quantity': 1, 'price_unit': work,
                'tax_ids': [Command.set(self.vat.ids)],
                'sale_line_ids': [Command.set(order.order_line.ids)],
            })],
            'retention_percent': retention,
            'retention_handover_date': handover,
            'advance_payment_id': payment.id if payment else False,
            'advance_recovery_percent': recovery,
        })
        if post:
            invoice.action_post()
        return invoice

    def _retention_balance(self, partner=None):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', self.company.retention_account_id.id),
            ('partner_id', '=', (partner or self.customer).id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('balance')), sum(lines.mapped('amount_residual'))

    def _release(self, amount=None, date='2026-12-31', invoices=None):
        wizard = self.env['retention.release.wizard'].create({
            'partner_id': self.customer.id, 'date': date,
            'release_mode': 'partial' if amount else 'full', 'amount': amount or 0.0})
        if invoices:
            for line in wizard.line_ids:
                line.selected = line.invoice_id in invoices
        wizard.action_release()
        return wizard

    def _report(self, as_of='2030-01-01', **kw):
        wizard = self.env['retention.report.wizard'].create({'as_of_date': as_of, 'company_ids': self.company.ids, **kw})
        wizard.action_generate()
        return wizard.line_ids

    # ------------------------------------------------------------------ the example
    def test_01_example_of_the_brief(self):
        order = self._sale_order()
        advance = self._advance(order)
        invoice = self._invoice(order, payment=advance)

        self.assertEqual(invoice.amount_untaxed, 100000.0)
        self.assertEqual(invoice.amount_total, 115000.0)
        self.assertEqual(invoice.retention_amount, 10000.0)
        self.assertEqual(invoice.advance_recovery_amount, 20000.0)
        self.assertEqual(invoice.amount_to_pay_now, 85000.0)

        # What the client owes now, and what is parked in the retention account.
        self.assertEqual(invoice.amount_residual, 85000.0)
        self.assertEqual(self._retention_balance()[0], 10000.0)
        retention_line = invoice._get_retention_line()
        self.assertEqual(retention_line.account_id, self.company.retention_account_id)
        self.assertEqual(str(retention_line.date_maturity), '2027-02-01')
        # The advance is matched: 180,000 left.
        self.assertEqual(advance._get_advance_line().amount_residual, -180000.0)
        self.assertEqual(invoice.advance_payment_id._get_advance_remaining(invoice.currency_id, invoice.date), 180000.0)
        # VAT is on the full work done.
        self.assertEqual(invoice.amount_tax, 15000.0)

        row = self._report()
        self.assertEqual(
            (row.total_invoiced, row.retention_held, row.retention_released, row.retention_balance,
             row.advance_received, row.advance_recovered, row.advance_balance),
            (115000.0, 10000.0, 0.0, 10000.0, 200000.0, 20000.0, 180000.0))
        self.assertEqual(row.sale_order_id, order)

    def test_01b_advance_payment_always_gets_a_journal_entry(self):
        journal = self.env['account.journal'].search(
            [('type', '=', 'bank'), ('company_id', '=', self.company.id)], limit=1)
        payment = self.env['account.payment'].create({
            'payment_type': 'inbound', 'partner_type': 'customer', 'partner_id': self.customer.id,
            'amount': 50000.0, 'journal_id': journal.id, 'is_advance': True})
        payment.outstanding_account_id = False  # what a database without outstanding account looks like
        payment.action_post()
        self.assertEqual(payment.move_id.state, 'posted')
        self.assertEqual(payment._get_advance_line().amount_residual, -50000.0)

    def test_01c_record_advance_from_confirmed_sales_order(self):
        order = self.env['sale.order'].create({
            'partner_id': self.customer.id, 'company_id': self.company.id,
            'order_line': [Command.create({
                'product_id': self.service.id, 'product_uom_qty': 10, 'price_unit': 100000.0})],
        })
        with self.assertRaises(UserError):
            order.action_record_advance_payment()
        order.action_confirm()
        self.assertEqual(order.advance_payment_count, 0)

        action = order.action_record_advance_payment()
        self.assertEqual((action['res_model'], action['view_mode']), ('account.payment', 'form'))
        defaults = action['context']
        self.assertEqual(defaults['default_partner_id'], self.customer.commercial_partner_id.id)
        self.assertEqual(defaults['default_company_id'], self.company.id)
        self.assertEqual(defaults['allowed_company_ids'][0], self.company.id)
        self.assertEqual(defaults['default_advance_sale_order_id'], order.id)
        self.assertTrue(defaults['default_is_advance'])
        self.assertEqual(order.advance_payment_count, 0)  # Opening the form does not record cash.

        form = Form(self.env['account.payment'].with_context(defaults))
        journal = self.env['account.journal'].search([
            ('type', '=', 'bank'), ('company_id', '=', self.company.id)], limit=1)
        form.journal_id = journal
        form.amount = 200000.0
        payment = form.save()
        self.assertEqual(payment.state, 'draft')
        self.assertEqual((payment.payment_type, payment.partner_type), ('inbound', 'customer'))
        self.assertEqual((payment.partner_id, payment.company_id), (self.customer, self.company))
        self.assertEqual((payment.is_advance, payment.advance_sale_order_id), (True, order))
        self.assertEqual(order.advance_payment_count, 1)

        payment.action_post()
        self.assertEqual(payment._get_advance_line().amount_residual, -200000.0)
        linked = order.action_view_advance_payments()
        self.assertEqual(self.env['account.payment'].search(linked['domain']), payment)

    def test_01d_advance_cannot_exceed_order_total_or_remaining_balance(self):
        order = self._sale_order(amount=1000.0)  # VAT 15%: total 1,150 SAR
        self.assertEqual(order.amount_total, 1150.0)
        journal = self.env['account.journal'].search([
            ('type', '=', 'bank'), ('company_id', '=', self.company.id)], limit=1)
        vals = {
            'payment_type': 'inbound', 'partner_type': 'customer',
            'partner_id': self.customer.id, 'journal_id': journal.id,
            'date': '2026-01-01', 'is_advance': True,
            'advance_sale_order_id': order.id,
        }
        with self.cr.savepoint():
            with self.assertRaises(ValidationError):
                self.env['account.payment'].create({**vals, 'amount': 2000.0})

        first = self.env['account.payment'].create({**vals, 'amount': 600.0})
        first.action_post()
        second = self.env['account.payment'].create({**vals, 'amount': 550.0})
        second.action_post()
        self.assertEqual(order.advance_payment_count, 2)
        with self.cr.savepoint():
            with self.assertRaises(ValidationError):
                self.env['account.payment'].create({**vals, 'amount': 1.0})

    def test_01e_batch_post_cannot_exceed_order_total(self):
        order = self._sale_order(amount=1000.0)
        journal = self.env['account.journal'].search([
            ('type', '=', 'bank'), ('company_id', '=', self.company.id)], limit=1)
        vals = {
            'payment_type': 'inbound', 'partner_type': 'customer',
            'partner_id': self.customer.id, 'journal_id': journal.id,
            'date': '2026-01-01', 'is_advance': True,
            'advance_sale_order_id': order.id, 'amount': 600.0,
        }
        first = self.env['account.payment'].create(vals)
        second = self.env['account.payment'].create(vals)
        with self.assertRaises(ValidationError):
            (first | second).action_post()
        self.assertEqual((first.state, second.state), ('draft', 'draft'))

    def test_01f_foreign_currency_advance_limit_uses_order_currency(self):
        usd = self.setup_other_currency('USD', rates=[('2026-01-01', 0.25)])
        order = self._sale_order(amount=1000.0)  # total 1,150 SAR
        journal = self.env['account.journal'].search([
            ('type', '=', 'bank'), ('company_id', '=', self.company.id)], limit=1)
        vals = {
            'payment_type': 'inbound', 'partner_type': 'customer',
            'partner_id': self.customer.id, 'journal_id': journal.id,
            'date': '2026-01-01', 'is_advance': True,
            'advance_sale_order_id': order.id, 'currency_id': usd.id,
        }
        # At this rate 300 USD is 1,200 SAR, above the 1,150 SAR order total.
        with self.cr.savepoint():
            with self.assertRaises(ValidationError):
                self.env['account.payment'].create({**vals, 'amount': 300.0})
        payment = self.env['account.payment'].create({**vals, 'amount': 250.0})
        payment.action_post()
        self.assertEqual(payment.currency_id, usd)

    def test_02_cannot_recover_more_than_the_advance(self):
        order = self._sale_order()
        advance = self._advance(order, amount=15000.0)
        with self.assertRaises(UserError):
            self._invoice(order, payment=advance)  # wants 20,000, only 15,000 available

    def test_02b_deductions_above_invoice_total_are_refused(self):
        order = self._sale_order()
        advance = self._advance(order)
        invoice = self._invoice(order, work=10000.0, payment=advance, post=False)  # total 11,500
        with self.assertRaises(Exception):
            invoice.write({'advance_recovery_type': 'fixed', 'advance_recovery_fixed': 12000.0})
        # A draft without lines can be saved: nothing can be judged yet.
        draft = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.customer.id, 'advance_payment_id': advance.id,
            'advance_recovery_type': 'fixed', 'advance_recovery_fixed': 100000.0})
        self.assertEqual(draft.state, 'draft')

    def test_03_ten_invoices_use_the_full_advance_then_release(self):
        order = self._sale_order()
        advance = self._advance(order)
        invoices = self.env['account.move']
        for month in range(1, 11):
            invoices |= self._invoice(order, payment=advance, date=f'2026-{month:02d}-15')
        self.assertEqual(advance._get_advance_line().amount_residual, 0.0)
        self.assertEqual(self._retention_balance()[0], 100000.0)
        # An 11th invoice cannot recover anything.
        with self.assertRaises(UserError):
            self._invoice(order, payment=advance, date='2026-12-01')

        self._release(date='2027-03-01')
        balance, residual = self._retention_balance()
        self.assertEqual((balance, residual), (0.0, 0.0))
        # The released amount is now a normal receivable, due on the release date.
        release = self.env['account.move'].search([('retention_entry_type', '=', 'release')])
        receivable = release.line_ids.filtered(lambda l: l.debit)
        self.assertEqual(receivable.debit, 100000.0)
        self.assertEqual(receivable.amount_residual, 100000.0)
        self.assertEqual(str(receivable.date_maturity), '2027-03-01')
        row = self._report()
        self.assertEqual((row.retention_held, row.retention_released, row.retention_balance),
                         (100000.0, 100000.0, 0.0))
        # As at a date before the release, it was still held.
        before = self._report(as_of='2027-02-01')
        self.assertEqual((before.retention_released, before.retention_balance), (0.0, 100000.0))
        self.assertEqual(before.advance_balance, 0.0)

    def test_04_partial_release_follows_handover_order(self):
        order = self._sale_order()
        first = self._invoice(order, handover='2027-01-01', date='2026-01-10')
        second = self._invoice(order, handover='2027-06-01', date='2026-02-10')
        self._release(amount=15000.0)
        self.assertEqual(first._get_retention_line().amount_residual, 0.0)
        self.assertEqual(second._get_retention_line().amount_residual, 5000.0)
        self.assertEqual((first.retention_released, first.retention_balance), (10000.0, 0.0))
        self.assertEqual((second.retention_released, second.retention_balance), (5000.0, 5000.0))
        with self.assertRaises(UserError):
            self._release(amount=50000.0)

    def test_04b_release_through_the_form_like_the_browser(self):
        order = self._sale_order()
        invoice = self._invoice(order)
        form = Form(self.env['retention.release.wizard'].with_context(default_partner_id=self.customer.id))
        form.date = '2027-03-01'
        form.release_mode = 'partial'
        form.amount = 4000.0
        wizard = form.save()
        wizard.action_release()
        self.assertEqual(invoice._get_retention_line().amount_residual, 6000.0)

    def test_05_credit_note_returns_in_proportion(self):
        order = self._sale_order()
        advance = self._advance(order)
        invoice = self._invoice(order, payment=advance)
        # Half credit note
        credit_note = invoice._reverse_moves([{'invoice_date': '2026-03-01'}])
        credit_note.invoice_line_ids.price_unit = 50000.0
        credit_note.action_post()
        self.assertEqual(credit_note.amount_total, 57500.0)
        self.assertEqual(invoice._get_retention_line().amount_residual, 5000.0)
        entry = credit_note.retention_entry_ids
        self.assertEqual((entry.retention_entry_retention, entry.retention_entry_advance), (5000.0, 10000.0))
        self.assertEqual(advance._get_advance_line().amount_residual, -190000.0)
        row = self._report()
        self.assertEqual((row.total_invoiced, row.retention_held, row.advance_recovered),
                         (57500.0, 5000.0, 10000.0))

        # The rest, with the credit note that cancels the invoice.
        second = invoice._reverse_moves([{'invoice_date': '2026-03-02'}])
        second.invoice_line_ids.price_unit = 50000.0
        second.action_post()
        self.assertEqual(invoice._get_retention_line().amount_residual, 0.0)
        self.assertEqual(advance._get_advance_line().amount_residual, -200000.0)
        row = self._report()
        self.assertEqual((row.total_invoiced, row.retention_held, row.advance_recovered, row.advance_balance),
                         (0.0, 0.0, 0.0, 200000.0))

    def test_05b_credit_note_can_be_edited_and_saved_in_the_form(self):
        order = self._sale_order()
        invoice = self._invoice(order)
        credit_note = invoice._reverse_moves([{'invoice_date': '2026-03-01'}])
        with Form(credit_note) as form:  # what the browser does: the handover date is not asked on credit notes
            with form.invoice_line_ids.edit(0) as line:
                line.price_unit = 50000.0
        credit_note.action_post()
        self.assertEqual(invoice._get_retention_line().amount_residual, 5000.0)

    def test_05c_returned_advance_can_be_recovered_again(self):
        order = self._sale_order()
        advance = self._advance(order, amount=30000.0)
        invoice = self._invoice(order, payment=advance)
        credit_note = invoice._reverse_moves([{'invoice_date': '2026-03-01'}])
        credit_note.invoice_line_ids.price_unit = 50000.0
        credit_note.action_post()
        self.assertEqual(advance._get_advance_line().amount_residual, -20000.0)
        self.assertEqual(advance._get_advance_remaining(invoice.currency_id, invoice.date), 20000.0)
        second_invoice = self._invoice(order, payment=advance, date='2026-04-01')
        self.assertEqual(second_invoice.advance_recovery_amount, 20000.0)
        self.assertEqual(advance._get_advance_line().amount_residual, 0.0)
        row = self._report()
        self.assertEqual((row.advance_received, row.advance_recovered, row.advance_balance),
                         (30000.0, 30000.0, 0.0))

    def test_05d_credit_note_reset_restores_advance_recovery_once(self):
        order = self._sale_order()
        advance = self._advance(order)
        invoice = self._invoice(order, payment=advance)
        credit_note = invoice._reverse_moves([{'invoice_date': '2026-03-01'}])
        credit_note.invoice_line_ids.price_unit = 50000.0
        credit_note.action_post()
        self.assertEqual(advance._get_advance_line().amount_residual, -190000.0)
        credit_note.button_draft()
        self.assertEqual(advance._get_advance_line().amount_residual, -180000.0)
        credit_note.action_post()
        self.assertEqual(advance._get_advance_line().amount_residual, -190000.0)
        row = self._report()
        self.assertEqual((row.advance_recovered, row.advance_balance), (10000.0, 190000.0))

    def test_05e_credit_note_restores_exhausted_advance_without_touching_other_invoices(self):
        order = self._sale_order()
        advance = self._advance(order, amount=40000.0)
        first = self._invoice(order, payment=advance)
        second = self._invoice(order, payment=advance, date='2026-03-01')
        self.assertEqual(advance._get_advance_line().amount_residual, 0.0)
        credit_note = first._reverse_moves([{'invoice_date': '2026-04-01'}])
        credit_note.invoice_line_ids.price_unit = 50000.0
        credit_note.action_post()
        self.assertEqual(advance._get_advance_line().amount_residual, -10000.0)
        self.assertEqual(second.amount_residual, 85000.0)
        row = self._report()
        self.assertEqual((row.advance_recovered, row.advance_balance), (30000.0, 10000.0))

    def test_06_credit_note_reset_to_draft_and_post_again(self):
        order = self._sale_order()
        invoice = self._invoice(order, payment=None)
        credit_note = invoice._reverse_moves([{'invoice_date': '2026-03-01'}])
        credit_note.action_post()
        self.assertEqual(invoice._get_retention_line().amount_residual, 0.0)
        credit_note.button_draft()
        self.assertEqual(invoice._get_retention_line().amount_residual, 10000.0)
        credit_note.action_post()
        self.assertEqual(self._retention_balance()[0], 0.0)
        self.assertEqual(len(credit_note._get_live_retention_entries()), 1)

    def test_07_reset_to_draft_and_post_again_does_not_duplicate(self):
        order = self._sale_order()
        advance = self._advance(order)
        invoice = self._invoice(order, payment=advance)
        first_adjustment = invoice._get_live_retention_entries('adjust')[:1]
        invoice.button_draft()
        self.assertEqual(first_adjustment.reversal_move_ids.date, first_adjustment.date)
        self.assertEqual(self._retention_balance()[0], 0.0)
        self.assertEqual(advance._get_advance_line().amount_residual, -200000.0)
        draft_row = self._report(as_of='2026-03-01')
        self.assertEqual((draft_row.retention_held, draft_row.advance_recovered), (0.0, 0.0))
        invoice.action_post()
        self.assertEqual(self._retention_balance()[0], 10000.0)
        self.assertEqual(advance._get_advance_line().amount_residual, -180000.0)
        self.assertEqual(invoice.amount_residual, 85000.0)
        self.assertEqual(len(invoice._get_live_retention_entries()), 1)
        # The first entry is kept, reversed.
        self.assertEqual(len(invoice.retention_entry_ids), 2)
        row = self._report()
        self.assertEqual((row.retention_held, row.advance_recovered), (10000.0, 20000.0))
        # Looking back at a date before the reset must give the same figures (no doubling).
        past = self._report(as_of='2026-03-01')
        self.assertEqual((past.retention_held, past.advance_recovered, past.advance_balance),
                         (10000.0, 20000.0, 180000.0))

    def test_08_cannot_reset_after_release(self):
        order = self._sale_order()
        invoice = self._invoice(order)
        self._release(amount=1000.0)
        with self.assertRaises(UserError):
            invoice.button_draft()

    def test_08b_reset_respects_locked_adjustment_date(self):
        order = self._sale_order()
        invoice = self._invoice(order)
        self.company.hard_lock_date = fields.Date.to_date('2026-02-28')
        with self.assertRaises(UserError):
            invoice.button_draft()
        self.assertEqual(invoice.state, 'posted')

    def test_09_lock_dates_are_respected(self):
        order = self._sale_order()
        self._invoice(order)
        self.company.hard_lock_date = fields.Date.to_date('2026-12-31')
        with self.assertRaises(UserError):
            self._release(date='2026-06-01')
        self._release(date='2027-01-05')

    def test_10_foreign_currency(self):
        usd = self.setup_other_currency('USD', rates=[('2026-01-01', 0.25), ('2026-03-01', 0.28)])
        order = self._sale_order()
        advance = self._advance(order, amount=200000.0)  # SAR
        invoice = self._invoice(order, work=100000.0, payment=advance, currency=usd, date='2026-02-01')
        self.assertEqual(invoice.amount_total, 115000.0)
        self.assertEqual(invoice.retention_amount, 10000.0)
        self.assertEqual(invoice.amount_residual, 85000.0)
        # Entry is balanced and uses the invoice's own rate: 10,000 USD at 0.25 = 40,000 SAR
        retention_line = invoice._get_retention_line()
        self.assertEqual(retention_line.currency_id, usd)
        expected = invoice.company_currency_id.round(10000.0 / invoice.invoice_currency_rate)
        self.assertEqual(retention_line.balance, expected)
        self.assertEqual(self._retention_balance()[0], expected)
        # Release in USD at another rate
        self._release(date='2026-03-15')
        self.assertEqual(self._retention_balance(), (0.0, 0.0))  # exchange difference included
        release = self.env['account.move'].search([('retention_entry_type', '=', 'release')])
        self.assertEqual(release.line_ids.filtered('debit').amount_currency, 10000.0)
        self.assertTrue(release.currency_id == usd)
        row = self._report()
        self.assertEqual((row.retention_balance, row.retention_released), (0.0, row.retention_held))

    def test_10a_foreign_currency_credit_note_restores_advance_at_invoice_rate(self):
        usd = self.setup_other_currency('USD', rates=[('2026-01-01', 0.25), ('2026-03-01', 0.28)])
        order = self._sale_order()
        advance = self._advance(order, amount=200000.0)
        invoice = self._invoice(order, payment=advance, currency=usd, date='2026-02-01')
        original_remaining = -advance._get_advance_line().amount_residual
        credit_note = invoice._reverse_moves([{'invoice_date': '2026-03-01'}])
        credit_note.invoice_line_ids.price_unit = 50000.0
        credit_note.action_post()
        returned_company = invoice._retention_to_company(10000.0)
        self.assertEqual((credit_note.retention_entry_ids.retention_entry_retention,
                          credit_note.retention_entry_ids.retention_entry_advance), (5000.0, 10000.0))
        self.assertEqual(-advance._get_advance_line().amount_residual,
                         original_remaining + returned_company)
        row = self._report()
        self.assertEqual(row.advance_balance, original_remaining + returned_company)
        self.assertEqual(row.advance_recovered, 200000.0 - row.advance_balance)

    def test_10b_release_of_a_foreign_invoice_among_local_ones(self):
        usd = self.setup_other_currency('USD', rates=[('2026-01-01', 0.25), ('2027-06-01', 0.28)])
        order = self._sale_order()
        local = self._invoice(order, handover='2027-01-01', date='2026-01-10')
        foreign = self._invoice(order, work=10000.0, currency=usd, date='2026-02-01', handover='2027-06-01')
        self._release(date='2027-07-01', invoices=foreign)  # the SAR invoice is listed first but not ticked
        release = self.env['account.move'].search([('retention_entry_type', '=', 'release')])
        self.assertEqual(release.currency_id, usd)
        self.assertEqual(foreign._get_retention_line().amount_residual_currency, 0.0)
        self.assertEqual(local._get_retention_line().amount_residual, 10000.0)

    def test_11_multi_company(self):
        order = self._sale_order()
        self._invoice(order)

        company_2 = self._create_company(name='Second Contractor')
        self.env.user.company_ids |= company_2
        self.setup_company_retention(company_2)
        env2 = self.env(context=dict(self.env.context, allowed_company_ids=[company_2.id, self.company.id]))
        self.assertNotEqual(company_2.retention_account_id, self.company.retention_account_id)

        wizard = env2['retention.release.wizard'].create({
            'company_id': company_2.id, 'partner_id': self.customer.id})
        self.assertFalse(wizard.line_ids)  # company 1's retention is not offered
        report = env2['retention.report.wizard'].create({'company_ids': [Command.set(company_2.ids)]})
        report.action_generate()
        self.assertFalse(report.line_ids)

        action = order.with_context(allowed_company_ids=[company_2.id, self.company.id]).action_record_advance_payment()
        self.assertEqual(action['context']['allowed_company_ids'], [self.company.id, company_2.id])

    def test_12_excel_export(self):
        order = self._sale_order()
        self._invoice(order)
        wizard = self.env['retention.report.wizard'].create({'as_of_date': '2030-01-01'})
        action = wizard.action_export_xlsx()
        self.assertIn('/web/content/', action['url'])

    def test_13_invoice_without_sale_order_groups_under_customer(self):
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.customer.id, 'invoice_date': '2026-02-01',
            'retention_percent': 10.0, 'retention_handover_date': '2027-02-01',
            'invoice_line_ids': [Command.create({'product_id': self.service.id, 'price_unit': 1000.0})],
        })
        invoice.action_post()
        row = self._report()
        self.assertFalse(row.sale_order_id)
        self.assertEqual(row.retention_held, invoice.retention_amount)

    def test_14_missing_inputs_block_posting(self):
        order = self._sale_order()
        invoice = self._invoice(order, handover=False, post=False)
        with self.assertRaises(UserError):
            invoice.action_post()
        self.assertEqual(invoice.state, 'draft')
        self.company.retention_account_id = False
        invoice.retention_handover_date = '2027-01-01'
        with self.assertRaises(UserError):
            invoice.action_post()

    def test_15_report_print_template_renders(self):
        order = self._sale_order()
        advance = self._advance(order)
        invoice = self._invoice(order, payment=advance)
        html = self.env['ir.actions.report']._render_qweb_html('account.account_invoices', invoice.ids)[0]
        self.assertIn(b'Amount to pay now', html)
        self.assertIn(b'Less retention', html)
