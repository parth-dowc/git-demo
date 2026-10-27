import unittest
from nesna_dev_pilot import config, parse_expression, split_tsql_fields, source_key, raw_name_for, find_unique

class PilotMappingTests(unittest.TestCase):
    def test_five_exact_destinations(self):
        self.assertEqual(set(config()), {'permcontract','permcontractfield','permcontractdisbursement','permledger','permnote'})

    def test_legacy_fields_counts_and_order(self):
        expected={'permcontract':6,'permcontractfield':3,'permcontractdisbursement':8,'permledger':2,'permnote':11}
        for name, spec in config().items():
            self.assertEqual(len(spec['fields']),expected[name])
            self.assertEqual([parse_expression(x)[2] for x in spec['fields']],
                             ['RemovedFromSource' if x.startswith('CAST') else x for x in spec['fields']])

    def test_cast_translation(self):
        self.assertEqual(parse_expression('cast(RemovedFromSource as int) RemovedFromSource'),('int','RemovedFromSource','RemovedFromSource'))
        self.assertEqual(parse_expression('CAST( RemovedFromSource AS int ) AS RemovedFromSource'),('int','RemovedFromSource','RemovedFromSource'))
        self.assertEqual(parse_expression('[ContractId]'),('column','ContractId','ContractId'))

    def test_reject_unexpected_transform(self):
        for x in ('Random() AS ContractId','SELECT * FROM secrets','ContractId;TRUNCATE PUBLIC.X'):
            with self.assertRaises(ValueError): parse_expression(x)

    def test_raw_name_case_sensitive_resolution(self):
        self.assertEqual(raw_name_for('perm.Contract',['permContract','ssas_SSASContractDimension']),'permContract')
        self.assertEqual(raw_name_for('perm.Contract',['perm__Contract']),'perm__Contract')
        with self.assertRaises(ValueError): raw_name_for('perm.Contract',['permContract','perm__Contract'])

    def test_correct_perm_contractfield_vs_contract_contractfield(self):
        self.assertEqual(source_key(config()['permcontractfield']['source']),('perm','contractfield'))
        self.assertNotEqual(source_key('contract.ContractField'),source_key(config()['permcontractfield']['source']))

    def test_tsql_splitter(self):
        self.assertEqual(split_tsql_fields('NoteId, cast(RemovedFromSource as int) RemovedFromSource,Note'),
                         ['NoteId','cast(RemovedFromSource as int) RemovedFromSource','Note'])
        self.assertEqual(split_tsql_fields("replace(a,',', '' ) as b, c"),["replace(a,',', '' ) as b",'c'])

    def test_ambiguous_columns(self):
        with self.assertRaises(ValueError):find_unique('ContractId',['CONTRACTID','contractid'],'test')

if __name__=='__main__': unittest.main()
