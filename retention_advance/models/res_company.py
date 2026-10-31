from odoo import _, fields, models
from odoo.exceptions import UserError


class ResCompany(models.Model):
    _inherit = 'res.company'

    retention_account_id = fields.Many2one(
        'account.account',
        string='Retention Receivable Account',
        check_company=True,
        domain="[('account_type', '=', 'asset_receivable')]",
        help="Receivable account that holds the amounts withheld by the client until handover.",
    )
    retention_default_percent = fields.Float(string='Default Retention %')

    def _retention_get_journal(self):
        """Journal of our entries: Miscellaneous Operations when there is one, else any general journal."""
        self.ensure_one()
        domain = [('type', '=', 'general'), ('company_id', '=', self.id)]
        journal = self.env['account.journal'].search(domain + [('code', '=', 'MISC')], limit=1) \
            or self.env['account.journal'].search(domain, limit=1)
        if not journal:
            raise UserError(_("No miscellaneous journal found for company %s.", self.name))
        return journal
