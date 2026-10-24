"""Declarative role bindings are checked independently of the agent's claims."""
import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from .common import tree_manifest, write_json

MD='{http://v8.1c.ru/8.3/MDClasses}'
V='{http://v8.1c.ru/8.1/data/core}'
IDENT=re.compile(r'^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9]*$')
class UnsupportedBindings(ValueError):
    pass

GROUPS={'Catalog':'Catalogs','Document':'Documents','AccountingRegister':'AccountingRegisters','InformationRegister':'InformationRegisters','ChartOfAccounts':'ChartsOfAccounts','Report':'Reports','AccumulationRegister':'AccumulationRegisters','ChartOfCharacteristicTypes':'ChartsOfCharacteristicTypes','CalculationRegister':'CalculationRegisters','ChartOfCalculationTypes':'ChartsOfCalculationTypes'}


def descriptor(src, ref):
    parts=ref.split('.') if isinstance(ref,str) else []
    if len(parts)!=2 or parts[0] not in GROUPS or not IDENT.fullmatch(parts[1]):
        raise ValueError('Invalid metadata reference: '+str(ref))
    path=src/GROUPS[parts[0]]/(parts[1]+'.xml')
    if not path.is_file():raise ValueError('Metadata object absent: '+ref)
    root=ET.parse(path).getroot().find(MD+parts[0])
    if root is None or root.findtext(MD+'Properties/'+MD+'Name')!=parts[1]:
        raise ValueError('Metadata descriptor does not match reference: '+ref)
    return root


REPORT_FIELDS={'bu_sales':('item','sold','quantity','cost','revenue','profit'),
               'ou_statement':('item','characteristic','opening','receipt','expense','closing','opening_qty','receipt_qty','expense_qty','closing_qty'),
               'ou_sales':('item','quantity','cost','revenue','profit'),
               'pr_bonus':('employee','seniority','percent','amount')}


def check_report(src, role, report):
    """A declared report: DCS main schema with period parameters, or an exported function (start, end) returning a table."""
    if role not in REPORT_FIELDS:raise ValueError('Unknown report role: '+role)
    if not isinstance(report,dict) or not isinstance(report.get('object'),str) or not report['object'].startswith('Report.'):raise ValueError('Report object must be Report.<name>: '+role)
    obj=descriptor(src,report['object']);name=report['object'].split('.')[1]
    fields=report.get('fields') or {}
    if not fields or set(fields)-set(REPORT_FIELDS[role]) or not all(IDENT.fullmatch(v) for v in fields.values()):raise ValueError('Report fields must map known roles to column names: '+role)
    if report.get('kind')=='dcs':
        if not (obj.findtext(MD+'Properties/'+MD+'MainDataCompositionSchema') or '').strip():raise ValueError('Report has no main data composition schema: '+role)
        params=report.get('parameters') or {}
        allowed={'period_start','period_end','period'}|({'department'} if role=='ou_statement' else set())
        if not params or set(params)-allowed or not all(IDENT.fullmatch(v) for v in params.values()):raise ValueError('DCS report parameters: period_start/period_end or period (and department for ou_statement): '+role)
        if role=='ou_statement' and 'department' not in params:raise ValueError('ou_statement needs the department parameter')
    elif report.get('kind')=='function':
        module={'object':'ObjectModule.bsl','manager':'ManagerModule.bsl'}.get(report.get('module'))
        if module is None or not IDENT.fullmatch(report.get('function','')):raise ValueError('Function report needs module object|manager and function: '+role)
        code=src/'Reports'/name/'Ext'/module
        text=code.read_text(encoding='utf-8-sig') if code.is_file() else ''
        if not re.search(r'(?im)^\s*Функция\s+'+re.escape(report['function'])+r'\s*\([^)]*\)\s*Экспорт',text):raise ValueError('Report function must be exported (start, end[, department for ou_statement]): '+role)
    else:raise ValueError('Report kind must be dcs or function: '+role)


def subconto_kinds(src, accounts):
    """Predefined subconto kinds of the account chart: '<chart of characteristic types>.<name>' -> value types."""
    chart=descriptor(src,accounts).findtext(MD+'Properties/'+MD+'ExtDimensionTypes') or ''
    if not chart.startswith('ChartOfCharacteristicTypes.'):return {}
    name=chart.split('.')[1];descriptor(src,chart)
    path=src/'ChartsOfCharacteristicTypes'/name/'Ext'/'Predefined.xml'
    if not path.is_file():return {}
    kinds={}
    for item in ET.parse(path).getroot().iter('{http://v8.1c.ru/8.3/xcf/predef}Item'):
        kind=item.findtext('{http://v8.1c.ru/8.3/xcf/predef}Name')
        # The type prefix is a namespace alias (cfg:, d4p1:, ...) for the current configuration.
        types={'cfg:'+t.text.split(':',1)[-1] for t in item.findall('{http://v8.1c.ru/8.3/xcf/predef}Type/'+V+'Type') if t.text}
        if kind and IDENT.fullmatch(kind):kinds[name+'.'+kind]=types
    return kinds


# Field roles of the public binding contract; anything else is the solver's extra description (owner decision 2026-10-13):
# recorded by extra_sections and ignored, never a reason to reject the submission.
FIELD_ROLES={'items':('article',),'receipt':('warehouse','item','quantity','price','amount'),'sale':('warehouse','item','quantity','price','amount'),
             'ledger':('item_debit','item_credit','warehouse_debit','warehouse_credit','quantity_debit','quantity_credit','amount')}


def strip_extras(bindings):
    """A copy of the binding without the entries outside the contract, and their paths (owner decisions 2026-10-13).

    Covers the main fields and reports and the section bindings: ou objects/fields, pr calculation types/fields and
    interface sections. Only names the contract does not know are dropped; a wrong value of a known name stays an error."""
    import copy
    from .ou import OBJECTS as OU_OBJECTS, FIELDS as OU_FIELDS
    from .pr import FIELDS as PR_FIELDS, ROLES as PR_ROLES
    from .interface import SECTIONS
    clean=copy.deepcopy(bindings);removed=[]
    def drop(container,known,prefix):
        if not isinstance(container,dict):return
        for key in [k for k in container if k not in known]:
            del container[key];removed.append(prefix+key)
    fields=clean.get('fields')
    drop(fields,FIELD_ROLES,'fields.')
    for role,names in FIELD_ROLES.items():drop((fields or {}).get(role),names,'fields.%s.'%role)
    drop(clean.get('reports'),REPORT_FIELDS,'reports.')
    ou=clean.get('ou')
    if isinstance(ou,dict):
        drop(ou.get('objects'),OU_OBJECTS,'ou.objects.')
        known={**{k:v for k,v in OU_FIELDS.items()},'policy_values':('fifo','lifo','average'),'properties':None}
        drop(ou.get('fields'),known,'ou.fields.')
        for role,names in known.items():
            if names is not None:drop((ou.get('fields') or {}).get(role),names,'ou.fields.%s.'%role)
    pr=clean.get('pr')
    if isinstance(pr,dict):
        drop(pr.get('calc_types'),PR_ROLES,'pr.calc_types.')
        drop(pr.get('fields'),PR_FIELDS,'pr.fields.')
        for role,names in PR_FIELDS.items():drop((pr.get('fields') or {}).get(role),names,'pr.fields.%s.'%role)
    interface=clean.get('interface')
    if isinstance(interface,dict):drop(interface.get('sections'),SECTIONS,'interface.sections.')
    return clean,removed


def extra_sections(bindings):
    """Binding entries outside the contract that the adapter ignores."""
    return strip_extras(bindings)[1]


def canonical_ledger(src, bindings):
    """Ledger roles named by a bare subconto kind ('Номенклатура') become '<chart of characteristic types>.<kind>'
    when the register has no physical field of that name and exactly one predefined kind of the account chart matches."""
    import copy
    ledger=((bindings.get('fields') or {}).get('ledger') or {})
    objects=bindings.get('objects') or {}
    if not ledger or not objects.get('accounts') or not objects.get('ledger'):return bindings,[]
    try:
        register=descriptor(src,objects['ledger']);kinds=subconto_kinds(src,objects['accounts'])
    except ValueError:return bindings,[]
    physical=set()
    for tag in ('Dimension','Resource','Attribute'):
        for n in register.findall(MD+'ChildObjects/'+MD+tag):
            name=n.findtext(MD+'Properties/'+MD+'Name');physical|={name,name+'Дт',name+'Кт'}
    result=copy.deepcopy(bindings);expanded=[]
    for role,path in ledger.items():
        if not role.startswith(('item_','warehouse_')) or not isinstance(path,str) or '.' in path or path in physical:continue
        matches=[k for k in kinds if k.split('.',1)[1]==path]
        if len(matches)==1:
            result['fields']['ledger'][role]=matches[0];expanded.append('fields.ledger.%s: %s -> %s'%(role,path,matches[0]))
    return result,expanded


def check_bindings(src, bindings):
    if bindings.get('schema_version')!=1:raise ValueError('Unsupported binding version')
    bindings=strip_extras(bindings)[0];ignored=set()
    for role in ('items','warehouses','receipt','sale','manual','ledger','accounts'):
        if not isinstance(bindings.get('objects'),dict) or role not in bindings['objects']:raise ValueError('binding.objects.%s is required'%role)
        ref=bindings['objects'][role]
        expected={'items':'Catalog','warehouses':'Catalog','receipt':'Document','sale':'Document','manual':'Document','ledger':'AccountingRegister','accounts':'ChartOfAccounts'}[role]
        if not ref.startswith(expected+'.'):raise ValueError('Incorrect object type for '+role)
        descriptor(src,ref)
    # Every path consists only of identifiers, never BSL/SQL or a filesystem path.
    for role,mappings in bindings['fields'].items():
        if 'fields.'+role in ignored:continue
        obj=descriptor(src,bindings['objects'][role])
        for name,path in mappings.items():
            if 'fields.%s.%s'%(role,name) in ignored:continue
            parts=path.split('.') if isinstance(path,str) else []
            if not 1<=len(parts)<=2 or not all(IDENT.fullmatch(p) for p in parts):raise ValueError('Invalid field path')
            if role=='ledger':continue # Physical record fields/subconto checked by trusted 1C metadata at runtime.
            node=obj
            for i,p in enumerate(parts):
                tag='TabularSection' if i<len(parts)-1 else 'Attribute'
                found=[c for c in node.findall(MD+'ChildObjects/'+MD+tag) if c.findtext(MD+'Properties/'+MD+'Name')==p]
                if p in ('Код','Наименование') and role=='items' and len(parts)==1:continue
                if len(found)!=1:raise ValueError('Field absent: '+role+'.'+path)
                node=found[0]
    # Resolve actual physical ledger fields, including debit/credit nonbalance resources.
    ledger=descriptor(src,bindings['objects']['ledger'])
    if ledger.findtext(MD+'Properties/'+MD+'ChartOfAccounts')!=bindings['objects']['accounts']:raise ValueError('Ledger/account chart mismatch')
    if ledger.findtext(MD+'Properties/'+MD+'Correspondence')!='true':raise ValueError('Double-entry register required')
    physical={}
    for tag in ('Dimension','Resource','Attribute'):
        for n in ledger.findall(MD+'ChildObjects/'+MD+tag):
            prop=n.find(MD+'Properties');name=prop.findtext(MD+'Name');types={x.text for x in prop.findall(MD+'Type/'+V+'Type')}
            suffixes=('Дт','Кт') if prop.findtext(MD+'Balance')=='false' and tag!='Attribute' else ('',)
            for suffix in suffixes:physical[name+suffix]=types
    subconto=subconto_kinds(src,bindings['objects']['accounts'])
    for role,path in bindings['fields']['ledger'].items():
        if 'fields.ledger.'+role in ignored:continue
        expected='xs:decimal' if role in ('quantity_debit','quantity_credit','amount') else 'cfg:'+bindings['objects']['items'].replace('Catalog.','CatalogRef.') if role.startswith('item_') else 'cfg:'+bindings['objects']['warehouses'].replace('Catalog.','CatalogRef.')
        if '.' in path and not role.startswith(('quantity_','amount')):
            # <chart of characteristic types>.<predefined kind>: the value of that subconto kind on the role's side.
            if path not in subconto:raise UnsupportedBindings('Ledger subconto is not a predefined kind of the account chart: '+path)
            if subconto[path]!={expected}:raise ValueError('Ledger subconto type mismatch: '+role)
            continue
        if path not in physical:raise UnsupportedBindings('Ledger field is neither a physical dimension/resource nor a predefined subconto kind: '+path)
        if physical[path]!={expected}:raise ValueError('Ledger field type mismatch: '+role)
    for role in ('receipt','sale','manual'):
        refs=descriptor(src,bindings['objects'][role]).find(MD+'Properties/'+MD+'RegisterRecords')
        if refs is None or not any(x.text==bindings['objects']['ledger'] for x in refs):raise ValueError('Document does not expose bound ledger: '+role)
    for role in ('receipt','sale'):
        fs=bindings['fields'][role]
        if any(fs[k].split('.')[0]!=fs['item'].split('.')[0] for k in ('quantity','price','amount')):raise ValueError('Receipt/sale fields must share one table')
    for role in ('goods','suppliers','customers','profit'):
        if not isinstance(bindings['account_codes'].get(role),str):raise ValueError('Missing account code '+role)
    analog=bindings['analogs']
    # `mode` is the solver's free-text description; the stored structure decides what the adapter can observe.
    if isinstance(analog.get('object'),str) and analog['object']==bindings['objects']['items']:
        # A tabular section of the items catalog: left is the owner (Ссылка), right is <section>.<attribute> holding an item.
        parts=str(analog.get('right','')).split('.')
        if analog.get('left') not in ('Ссылка','Ref') or len(parts)!=2 or not all(IDENT.fullmatch(p) for p in parts):
            # Another representation inside the catalog (attributes, a single field): not observable by this adapter.
            raise UnsupportedBindings('Adapter interprets analogs in the items catalog only as a tabular section: left Ссылка, right <section>.<attribute>')
        items=descriptor(src,bindings['objects']['items'])
        section=[t for t in items.findall(MD+'ChildObjects/'+MD+'TabularSection') if t.findtext(MD+'Properties/'+MD+'Name')==parts[0]]
        if len(section)!=1:raise ValueError('Analog tabular section absent: '+parts[0])
        column=[a for a in section[0].findall(MD+'ChildObjects/'+MD+'Attribute') if a.findtext(MD+'Properties/'+MD+'Name')==parts[1]]
        expected='cfg:'+bindings['objects']['items'].replace('Catalog.','CatalogRef.')
        if len(column)!=1 or {t.text for t in column[0].findall(MD+'Properties/'+MD+'Type/'+V+'Type')}!={expected}:
            raise ValueError('Analog column must hold the item: '+analog['right'])
    elif not isinstance(analog.get('object'),str) or not analog['object'].startswith('InformationRegister.'):
        raise UnsupportedBindings('Adapter interprets analogs stored as an information register pair only: '+str(analog.get('object')))
    else:
        r=descriptor(src,analog['object'])
        for field in ('left','right'):
            if not IDENT.fullmatch(analog[field]):raise ValueError('Invalid analog field')
            if not any(n.findtext(MD+'Properties/'+MD+'Name')==analog[field] for tag in ('Dimension','Attribute','Resource') for n in r.findall(MD+'ChildObjects/'+MD+tag)):
                raise ValueError('Analog field absent')
    for role,report in (bindings.get('reports') or {}).items():
        if 'reports.'+role not in ignored:check_report(src,role,report)
    demo=bindings.get('demo_data')
    if demo is not None:
        # Optional by schema, required by the exam rule М4.32: an exported server procedure that fills demo data.
        parts=demo.get('procedure','').split('.') if isinstance(demo,dict) else []
        if len(parts)!=3 or parts[0]!='CommonModule' or not all(IDENT.fullmatch(x) for x in parts[1:]):raise ValueError('demo_data.procedure must be CommonModule.<module>.<procedure>')
        module=src/'CommonModules'/(parts[1]+'.xml')
        if not module.is_file():raise ValueError('Demo data module absent: '+parts[1])
        props=ET.parse(module).getroot().find(MD+'CommonModule/'+MD+'Properties')
        if props is None or props.findtext(MD+'Server')!='true':raise ValueError('Demo data module must run on the server')
        code=(src/'CommonModules'/parts[1]/'Ext'/'Module.bsl')
        text=code.read_text(encoding='utf-8-sig') if code.is_file() else ''
        if not re.search(r'(?im)^\s*Процедура\s+'+re.escape(parts[2])+r'\s*\(\s*\)\s*Экспорт',text):raise ValueError('Demo data procedure must be an exported procedure without parameters: '+parts[2])
    for role,fields in bindings.get('defaults',{}).items():
        if role not in ('receipt','sale','manual'):raise ValueError('Invalid defaults role')
        obj=descriptor(src,bindings['objects'][role])
        for field,value in fields.items():
            if not IDENT.fullmatch(field) or not any(x.findtext(MD+'Properties/'+MD+'Name')==field for x in obj.findall(MD+'ChildObjects/'+MD+'Attribute')):raise ValueError('Defaults attribute absent')
            if not isinstance(value,dict) or set(value)!={'catalog','name'} or not isinstance(value['name'],str):raise ValueError('Unsupported defaults value')
            if not value['catalog'].startswith('Catalog.'):raise ValueError('Defaults can bind catalog values only')
            descriptor(src,value['catalog'])
    return True


def validate(workspace, public_manifest, require_ready=True):
    ws=Path(workspace)
    for path,expected in public_manifest.items():
        from .common import digest
        if not (ws/path).is_file() or digest(ws/path)!=expected:raise ValueError('Public input changed: '+path)
    out=ws/'out';tree_manifest(out)
    if require_ready:
        ready=json.loads((out/'submission.ready.json').read_text(encoding='utf-8'))
        if ready!={'schema_version':1,'status':'ready'}:raise ValueError('Invalid readiness signal')
    src=out/'src'
    if not (src/'Configuration.xml').is_file():raise ValueError('No exported Configuration.xml')
    bindings=json.loads((out/'binding.json').read_text(encoding='utf-8'))
    bindings,_=canonical_ledger(src,bindings)
    check_bindings(src,bindings)
    return bindings


def freeze(workspace,target):
    target=Path(target)
    if target.exists():raise ValueError('Frozen artifact directory already exists')
    src=Path(workspace)/'out'
    manifest=tree_manifest(src) # Reject symlinks before copying, including dangling links.
    shutil.copytree(src,target,symlinks=False)
    if tree_manifest(target)!=manifest:raise ValueError('Submission changed during freeze')
    for p in target.rglob('*'):
        if p.is_file():p.chmod(0o444)
    write_json(target.parent/'artifact-manifest.json',manifest)
    return manifest
