from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    retention_account_id = fields.Many2one(
        related='company_id.retention_account_id', readonly=False,
        check_company=True,
        domain="[('account_type', '=', 'asset_receivable')]",
    )
    retention_default_percent = fields.Float(
        related='company_id.retention_default_percent', readonly=False)
