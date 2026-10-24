import copy,json,tempfile,unittest,shutil
from pathlib import Path
from exam_bench.common import tree_manifest
from exam_bench.submission import validate,check_bindings
from exam_bench.report import render
from fixtures import source,BINDING
class ContractTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.p=Path(self.temp.name)
        source(self.p/'out'/'src')
        self.b=copy.deepcopy(BINDING)
        (self.p/'out'/'binding.json').write_text(json.dumps(self.b))
        (self.p/'out'/'submission.ready.json').write_text(json.dumps({'schema_version':1,'status':'ready'}))
        (self.p/'task').mkdir();(self.p/'task'/'statement.txt').write_text('immutable')
        self.manifest={'task/'+k:v for k,v in tree_manifest(self.p/'task').items()}
    def tearDown(self):self.temp.cleanup()
    def test_valid_surrender(self):self.assertEqual(validate(self.p,self.manifest),self.b)
    def test_missing_source(self):
        (self.p/'out'/'src'/'Configuration.xml').unlink()
        with self.assertRaisesRegex(ValueError,'Configuration'):validate(self.p,self.manifest)
    def test_public_input_modified(self):
        (self.p/'task'/'statement.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'Public input'):validate(self.p,self.manifest)
    def test_fake_object(self):
        self.b['objects']['sale']='Document.DoesNotExist'
        with self.assertRaisesRegex(ValueError,'absent'):check_bindings(self.p/'out'/'src',self.b)
    def test_invalid_ready_signal(self):
        (self.p/'out'/'submission.ready.json').write_text('{"status":"done"}')
        with self.assertRaisesRegex(ValueError,'readiness'):validate(self.p,self.manifest)
    def test_fake_ledger_field(self):
        self.b['fields']['ledger']['amount']='ExpectedAmountFromAgent'
        with self.assertRaisesRegex(ValueError,'Ledger field'):check_bindings(self.p/'out'/'src',self.b)
    def test_unsupported_representation_is_not_subject_logic_failure(self):
        from exam_bench.submission import UnsupportedBindings
        self.b['analogs']['object']='Catalog.Items'
        with self.assertRaises(UnsupportedBindings):check_bindings(self.p/'out'/'src',self.b)
    def test_analog_mode_is_description_not_gate(self):
        self.b['analogs']['mode']='undirected_edges_transitive_closure'
        self.assertTrue(check_bindings(self.p/'out'/'src',self.b))
    def test_subconto_binding_is_adapter_limit(self):
        from exam_bench.submission import UnsupportedBindings
        self.b['fields']['ledger']['item_debit']='СубконтоДт1'
        with self.assertRaises(UnsupportedBindings):check_bindings(self.p/'out'/'src',self.b)
    def subconto_chart(self, warehouse_type='cfg:CatalogRef.Warehouses'):
        src=self.p/'out'/'src';md='xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core"'
        (src/'ChartsOfAccounts'/'Accounts.xml').write_text(f'<MetaDataObject {md}><ChartOfAccounts><Properties><Name>Accounts</Name><ExtDimensionTypes>ChartOfCharacteristicTypes.Kinds</ExtDimensionTypes></Properties></ChartOfAccounts></MetaDataObject>')
        (src/'ChartsOfCharacteristicTypes'/'Kinds'/'Ext').mkdir(parents=True)
        (src/'ChartsOfCharacteristicTypes'/'Kinds.xml').write_text(f'<MetaDataObject {md}><ChartOfCharacteristicTypes><Properties><Name>Kinds</Name></Properties></ChartOfCharacteristicTypes></MetaDataObject>')
        item=lambda name,typ:f'<Item id="1"><Name>{name}</Name><Type><v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:{typ.split(":")[1]}</v8:Type></Type></Item>'
        (src/'ChartsOfCharacteristicTypes'/'Kinds'/'Ext'/'Predefined.xml').write_text('<PredefinedData xmlns="http://v8.1c.ru/8.3/xcf/predef" xmlns:v8="http://v8.1c.ru/8.1/data/core">'+item('Item','cfg:CatalogRef.Items')+item('Store',warehouse_type)+'</PredefinedData>')
        self.b['fields']['ledger'].update(item_debit='Kinds.Item',item_credit='Kinds.Item',warehouse_debit='Kinds.Store',warehouse_credit='Kinds.Store')
    def test_predefined_subconto_kinds_bind_item_and_warehouse(self):
        self.subconto_chart()
        self.assertTrue(check_bindings(self.p/'out'/'src',self.b))
        from exam_bench.grader import code
        script=code(self.b,{'analogs':[],'receipts':[],'manual':('A','W1',1,10),'sale':[]})
        self.assertIn('Дв.СубконтоДт[ПланыВидовХарактеристик.Kinds.Item] = Товары["A"]',script)
        self.assertIn('SubcontoValue(Стр, "Кт", ПланыВидовХарактеристик.Kinds.Store)',script)
    def test_subconto_kind_type_is_checked(self):
        self.subconto_chart(warehouse_type='cfg:CatalogRef.Items')
        with self.assertRaisesRegex(ValueError,'subconto type mismatch'):check_bindings(self.p/'out'/'src',self.b)
    def test_unknown_subconto_kind_is_adapter_limit(self):
        from exam_bench.submission import UnsupportedBindings
        self.subconto_chart();self.b['fields']['ledger']['item_debit']='Kinds.Absent'
        with self.assertRaises(UnsupportedBindings):check_bindings(self.p/'out'/'src',self.b)
    def test_sections_outside_contract_are_ignored_and_listed(self):
        from exam_bench.submission import extra_sections
        self.b['fields']['manual']={'account_debit':'Records.AccountDr'}
        self.b['fields']['ledger']['account_debit']='СчетДт'
        self.b['fields']['receipt']['comment']='NoSuchAttribute'
        self.b['reports']={'free_text':{'object':'Report.Absent'}}
        self.assertTrue(check_bindings(self.p/'out'/'src',self.b))
        self.assertEqual(sorted(extra_sections(self.b)),['fields.ledger.account_debit','fields.manual','fields.receipt.comment','reports.free_text'])
    def test_section_extras_are_stripped_but_missing_roles_stay_errors(self):
        from exam_bench.submission import strip_extras
        b={'fields':{'items':{'article':'A','x':'y'}},'ou':{'objects':{'stock':'AccumulationRegister.S','junk':'Catalog.J'},'fields':{'sale':{'item':'Товары.Номенклатура','property':'Товары.Свойство'},'extra_role':{'a':'b'}}},
           'pr':{'calc_types':{'tariff':'ChartOfCalculationTypes.В.Оклад','vacation':'ChartOfCalculationTypes.В.Отпуск'}},'interface':{'sections':{'ou':'Subsystem.О','forms':'Subsystem.Ф'}}}
        clean,removed=strip_extras(b)
        self.assertEqual(sorted(removed),['fields.items.x','interface.sections.forms','ou.fields.extra_role','ou.fields.sale.property','ou.objects.junk','pr.calc_types.vacation'])
        self.assertEqual(clean['pr']['calc_types'],{'tariff':'ChartOfCalculationTypes.В.Оклад'})
        self.assertIn('forms',b['interface']['sections'])  # the frozen binding itself is never changed
    def test_bare_subconto_kind_name_is_expanded_once_unique(self):
        from exam_bench.submission import canonical_ledger
        self.subconto_chart()
        self.b['fields']['ledger'].update(item_debit='Item',warehouse_credit='Store')
        clean,expanded=canonical_ledger(self.p/'out'/'src',self.b)
        # Item is a physical dimension of the fixture ledger, so it stays; Store becomes the predefined kind.
        self.assertEqual((clean['fields']['ledger']['item_debit'],clean['fields']['ledger']['warehouse_credit']),('Item','Kinds.Store'))
        self.assertEqual(len(expanded),1)
        self.assertTrue(check_bindings(self.p/'out'/'src',clean))
    def test_validate_expands_bare_subconto_before_check_without_rewriting_submission(self):
        self.subconto_chart()
        self.b['fields']['ledger'].update(warehouse_debit='Store',warehouse_credit='Store')
        path=self.p/'out'/'binding.json'
        path.write_text(json.dumps(self.b))
        before=tree_manifest(self.p/'out')
        validated=validate(self.p,self.manifest)
        self.assertEqual(validated['fields']['ledger']['warehouse_debit'],'Kinds.Store')
        self.assertEqual(validated['fields']['ledger']['warehouse_credit'],'Kinds.Store')
        self.assertEqual(tree_manifest(self.p/'out'),before)
    def test_validate_checks_type_of_expanded_subconto(self):
        self.subconto_chart(warehouse_type='cfg:CatalogRef.Items')
        self.b['fields']['ledger']['warehouse_debit']='Store'
        (self.p/'out'/'binding.json').write_text(json.dumps(self.b))
        with self.assertRaisesRegex(ValueError,'subconto type mismatch'):
            validate(self.p,self.manifest)
    def test_validate_rejects_unknown_bare_subconto(self):
        from exam_bench.submission import UnsupportedBindings
        self.subconto_chart()
        self.b['fields']['ledger']['warehouse_debit']='MissingStore'
        (self.p/'out'/'binding.json').write_text(json.dumps(self.b))
        with self.assertRaises(UnsupportedBindings):
            validate(self.p,self.manifest)
    def test_analogs_in_a_tabular_section_of_the_items_catalog(self):
        src=self.p/'out'/'src';md='xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core"'
        (src/'Catalogs'/'Items.xml').write_text(f'<MetaDataObject {md}><Catalog><Properties><Name>Items</Name></Properties><ChildObjects>'
            '<Attribute><Properties><Name>Article</Name><Type><v8:Type>xs:string</v8:Type></Type></Properties></Attribute>'
            '<TabularSection><Properties><Name>Analogs</Name></Properties><ChildObjects><Attribute><Properties><Name>Item</Name>'
            '<Type><v8:Type>cfg:CatalogRef.Items</v8:Type></Type></Properties></Attribute></ChildObjects></TabularSection></ChildObjects></Catalog></MetaDataObject>')
        self.b['analogs']={'mode':'table','object':'Catalog.Items','left':'Ссылка','right':'Analogs.Item'}
        self.assertTrue(check_bindings(src,self.b))
        self.b['analogs']['right']='Analogs.Missing'
        with self.assertRaisesRegex(ValueError,'Analog column'):check_bindings(src,self.b)
    def test_missing_required_object_is_named(self):
        del self.b['objects']['manual']
        with self.assertRaisesRegex(ValueError,'binding.objects.manual is required'):check_bindings(self.p/'out'/'src',self.b)
    def test_readable_report_deterministic(self):
        r={'run_id':'test','model':{'requested':'gpt-example','provenance':'requested_only'},'harness':'none','process':{'status':'timeout'},'time':{'solve_ms':1000,'prepare_ms':10,'grade_ms':None},'metrics':{'tokens':{'input':100,'cached_input':70,'output':20,'reasoning_output':10,'total':120,'coverage':'partial'},'agents':{'spawned_total':None},'limitations':['in-flight usage unknown']},'billing':{'channel':'subscription','actual_charge':None,'api_equivalent':None},'submission':{'valid':False,'error':'missing signal'},'grade':{'whole_ticket':'not_evaluated','rules':[]},'comparison_eligible':False}
        self.assertEqual(render(r),render(json.loads(json.dumps(r))))
        self.assertIn('нет данных',render(r));self.assertIn('not_evaluated',render(r))
