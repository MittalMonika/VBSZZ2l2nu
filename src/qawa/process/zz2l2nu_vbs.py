import awkward as ak
import numpy as np
import hist
import yaml
import os 

from coffea import processor
from coffea.nanoevents.methods import candidate

from coffea.analysis_tools import Weights, PackedSelection
from coffea.lumi_tools import LumiMask

from qawa.roccor import rochester_correction
from qawa.applyGNN import applyGNN
from qawa.btag import BTVCorrector, btag_id
from qawa.jme import JMEUncertainty, update_collection
from qawa.common import pileup_weights, ewk_corrector, met_phi_xy_correction, theory_ps_weight, theory_pdf_weight, trigger_rules

def build_leptons(muons, electrons):
    # select tight/loose muons
    tight_muons_mask = (
        (muons.pt             >  20. ) &
        (np.abs(muons.eta)    <  2.4 ) &
        (np.abs(muons.dxy)    <  0.02) &
        (np.abs(muons.dz )    <  0.1 ) &
        (muons.pfRelIso04_all <= 0.15) & 
        muons.tightId
    )
    tight_muons = muons[tight_muons_mask]
    loose_muons = muons[
        ~tight_muons_mask &
        (muons.pt            >  7.  ) &
        (np.abs(muons.eta)   <  2.4 ) &
        (muons.pfRelIso04_all<= 0.15) &
        muons.looseId   
    ]
    # select tight/loose electron
    tight_electrons_mask = (
        (electrons.pt           > 20.) &
        (np.abs(electrons.eta)  < 2.5) &
        electrons.mvaFall17V2Iso_WP90
    )
    tight_electrons = electrons[tight_electrons_mask]
    loose_electrons = electrons[
        ~tight_electrons_mask &
        (electrons.pt           > 7. ) &
        (np.abs(electrons.eta)  < 2.5) &
        electrons.mvaFall17V2Iso_WPL
    ]
    # contruct a lepton object
    tight_leptons = ak.with_name(ak.concatenate([tight_muons, tight_electrons], axis=1), 'PtEtaPhiMCandidate')
    loose_leptons = ak.with_name(ak.concatenate([loose_muons, loose_electrons], axis=1), 'PtEtaPhiMCandidate')

    return tight_leptons, loose_leptons

def build_htaus(tau, lepton):
    base = (
        (tau.pt         > 20. ) & 
        (np.abs(tau.eta)< 2.3 ) & 
        (tau.decayMode != 5   ) & 
        (tau.decayMode != 6   )
    )
    overlap_leptons = ak.any(
        tau.metric_table(lepton) <= 0.4,
        axis=2
    )
    return tau[base & ~overlap_leptons]

def build_photons(photons):
    base = (
        (photon.pt          > 20. ) & 
        (np.abs(photon.eta) < 2.5 )
    )
    # MVA ID
    tight_photons = photons[base & photon.mvaID_WP90]
    loose_photons = photons[base & photon.mvaID_WP80 & ~photon.mvaID_WP90]
    
    # cut based ID
    return tight_photons, loose_photons


class zzinc_processor(processor.ProcessorABC):
    def __init__(self, era: str ='2018'):
        self._era = era
        
        jec_tag = ''
        jer_tag = ''
        if self._era == '2016':
            jec_tag = 'Summer19UL18_V5_MC'
            jer_tag = 'Summer19UL18_JRV2_MC'
        elif self._era == '2017':
            jec_tag = 'Summer19UL18_V5_MC'
            jer_tag = 'Summer19UL18_JRV2_MC'
        elif self._era == '2018':
            jec_tag = 'Summer19UL18_V5_MC'
            jer_tag = 'Summer19UL18_JRV2_MC'
        else:
            print('error')
        
        self.btag_wp = 'M'
        self.zmass = 91.1873 # GeV 
        self._btag = BTVCorrector(era=era, wp=self.btag_wp)
        self._jmeu = JMEUncertainty(jec_tag, jer_tag)
        self._purw = pileup_weights(era=self._era)
        
        
        _data_path = 'qawa/data'
        _data_path = os.path.join(os.path.dirname(__file__), '../data')
        self._json = {
            '2018': LumiMask(f'{_data_path}/json/{era}/Cert_314472-325175_13TeV_Legacy2018_Collisions18_JSON.txt'),
            '2017': LumiMask(f'{_data_path}/json/{era}/Cert_294927-306462_13TeV_UL2017_Collisions17_GoldenJSON.txt'),
            '2016': LumiMask(f'{_data_path}/json/{era}/Cert_294927-306462_13TeV_UL2017_Collisions17_GoldenJSON.txt'),

        }
        with open(f'{_data_path}/{era}-trigger-rules.yaml') as ftrig:
            self._triggers = yaml.load(ftrig, Loader=yaml.FullLoader)
            
        with open(f'{_data_path}/eft-names.dat') as eft_file:
            self._eftnames = [n.strip() for n in eft_file.readlines()]
            
        self.build_histos = lambda: {
            'dilep_mt': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, 1000, name="dilep_mt", label="$M_{T}$ (GeV)"),
                hist.storage.Weight()
            ), 
            'met': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, 1000, name="met", label="$p_{T}^{miss}$ (GeV)"),
                hist.storage.Weight()
            ),
            'njets': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(5, 0, 5, name="njets", label="$N_{jet}$ ($p_{T}>30$ GeV)"),
                hist.storage.Weight()
            ), 
            'bjets': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(5, 0, 5, name="bjets", label="$N_{b-jet}$ ($p_{T}>30$ GeV)"),
                hist.storage.Weight()
            ),
            'dphi_met_ll': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, 1, name="dphi_met_ll", label="$\Delta \phi(\ell\ell,\vec p_{T}^{miss})/\pi$"),
                hist.storage.Weight()
            ),
            'gnn_score': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, 1, name="gnn_score", label="gnn_score"),
                hist.storage.Weight()
            ),
        }
        
    def process_shift(self, event, shift_name:str=''):
        dataset = event.metadata['dataset']
        is_data = event.metadata.get("is_data")
        selection = PackedSelection()
        weights = Weights(len(event), storeIndividual=True)
        
        histos = self.build_histos()
        
        if is_data:
            selection.add('lumimask', self._json[self._era](event.run, event.luminosityBlock))
            selection.add('triggers', trigger_rules(event, self._triggers, self._era))
        else:
            selection.add('lumimask', np.ones(len(event), dtype='bool'))
            selection.add('triggers', np.ones(len(event), dtype='bool'))
        
        # MET filters
        selection.add(
            'metfilter',
            event.Flag.METFilters &
            event.Flag.HBHENoiseFilter &
            event.Flag.HBHENoiseIsoFilter & 
            event.Flag.EcalDeadCellTriggerPrimitiveFilter & 
            event.Flag.goodVertices & 
            event.Flag.eeBadScFilter & 
            event.Flag.globalTightHalo2016Filter &
            event.Flag.BadChargedCandidateFilter & 
            event.Flag.BadPFMuonFilter
        ) 
        
        tight_lep, loose_lep = build_leptons(
            event.Muon,
            event.Electron
        )
        
        had_taus = build_htaus(event.Tau, tight_lep)
        
        ntight_lep = ak.num(tight_lep)
        nloose_lep = ak.num(loose_lep)
        nhtaus_lep = ak.num(had_taus)
        
        jets = event.Jet
        overlap_leptons = ak.any(
            jets.metric_table(tight_lep) <= 0.4,
            axis=2
        )
        
        jet_mask = (
            ~overlap_leptons & 
            (jets.pt>30.0) & 
            (np.abs(jets.eta) < 4.7) & 
            (jets.jetId >= 6) # tight JetID 7(2016) and 6(2017/8)
        )
        
        jet_btag = (event.Jet.btagDeepFlavB > btag_id(self.btag_wp, self._era))
        
        good_jets = jets[~jet_btag & jet_mask]
        good_bjet = jets[jet_btag & jet_mask & (np.abs(jets.eta)<2.4)]
        
        ngood_jets  = ak.num(jets[~jet_btag & jet_mask])
        ngood_bjets = ak.num(jets[jet_btag & jet_mask & (np.abs(jets.eta)<2.4)])
        event['ngood_bjets'] = ngood_bjets
        event['ngood_jets']  = ngood_jets
        
        selection.add('0bjets', ngood_bjets ==0 )
        selection.add('0bjets_inc', ngood_bjets >=0 ) # at least
        selection.add('1bjets_inc', ngood_bjets >=1 ) # at least
        selection.add('0njets', ngood_jets  ==0 )
        selection.add('1njets', ngood_jets  ==1 )
        selection.add('01njets', ngood_jets  <=1 )
        selection.add('012njets', ngood_jets  <=2 )
        selection.add('2njets_inc', ngood_jets  >=2 ) # at least
        selection.add('0htaus', nhtaus_lep  ==0 ) # veto hadronic taus
        
        # lepton quantities
        def z_lepton_pair(leptons):
            pair = ak.combinations(leptons, 2, axis=1, fields=['l1', 'l2'])
            mass = (pair.l1 + pair.l2).mass
            cand = ak.local_index(mass, axis=1) == ak.argmin(np.abs(mass - self.zmass), axis=1)

            extra_lepton = leptons[(
                ~ak.any(leptons.metric_table(pair[cand].l1) <= 0.01, axis=2) & 
                ~ak.any(leptons.metric_table(pair[cand].l2) <= 0.01, axis=2) )
            ]
            return pair[cand], extra_lepton, cand
        
        dilep, extra_lep, z_cand_mask = z_lepton_pair(tight_lep)
        
        lead_lep = ak.firsts(ak.where(dilep.l1.pt >  dilep.l2.pt, dilep.l1, dilep.l2),axis=1)
        subl_lep = ak.firsts(ak.where(dilep.l1.pt <= dilep.l2.pt, dilep.l1, dilep.l2),axis=1)
        
        dilep_p4 = (lead_lep + subl_lep)
        dilep_m  = dilep_p4.mass
        dilep_pt = dilep_p4.pt
        
        # high level observables
        p4_met = ak.zip(
            {
                "pt": event.MET.pt,
                "eta": ak.zeros_like(event.MET.pt),
                "phi": event.MET.phi,
                "mass": ak.zeros_like(event.MET.pt),
                "charge": ak.zeros_like(event.MET.pt),
            },
            with_name="PtEtaPhiMCandidate",
            behavior=candidate.behavior,
        )
        emu_met = ak.firsts(extra_lep, axis=1) + p4_met
        
        dilep_et = np.sqrt(dilep_pt**2 + dilep_m**2)
        dilep_mt = ak.where(
            ntight_lep==3,
            np.sqrt((dilep_et + emu_met.pt)**2 - (dilep_p4.pvec + emu_met.pvec).p2),
            np.sqrt((dilep_et +  p4_met.pt)**2 - (dilep_p4.pvec +  p4_met.pvec).p2)
        )
        
        dphi_ll = lead_lep.delta_phi(subl_lep)
        deta_ll = np.abs(lead_lep.eta - subl_lep.eta)
        dR_ll   = dilep.l1.delta_r(dilep.l2)
        dphi_met_ll    = ak.where(ntight_lep==3, dilep_p4.delta_phi(emu_met), dilep_p4.delta_phi(p4_met))
        vector_balance = ak.where(ntight_lep==3, (emu_met - dilep_p4).pt/dilep_p4.pt, (p4_met - dilep_p4).pt/dilep_p4.pt)
        scalar_balance = ak.where(ntight_lep==3, emu_met.pt/dilep_p4.pt, p4_met.pt/dilep_p4.pt)
        
        event['met'     ] = p4_met.pt
        event['dilep_mt'] = dilep_mt
        event['njets'   ] = ngood_jets
        event['bjets'   ] = ngood_bjets
        event['dphi_met_ll'] = dphi_met_ll/np.pi
        event['leading_lep_pt'] = lead_lep.pt
        event['leading_lep_eta'] = lead_lep.eta
        event['leading_lep_phi'] = lead_lep.phi
        event['trailing_lep_pt'] = subl_lep.pt
        event['trailing_lep_eta'] = subl_lep.eta
        event['trailing_lep_phi'] = subl_lep.phi
                
        
        # build selections
        selection.add('2lep', (ntight_lep==2) & (nloose_lep==0) & (ak.firsts(tight_lep).pt>25))
        selection.add('3lep', (ntight_lep==3) & (nloose_lep==0) & (ak.firsts(tight_lep).pt>25))
        selection.add('4lep', ((ntight_lep + nloose_lep) == 4 ) & (ak.firsts(tight_lep).pt>25))
        selection.add('OSSF', ak.fill_none((lead_lep.pdgId + subl_lep.pdgId)==0, False))
        selection.add('OF'  , ak.fill_none(np.abs(lead_lep.pdgId) != np.abs(subl_lep.pdgId), False))
        
        
        # kinematic selections
        selection.add('dilep_pt_30'   , ak.fill_none(dilep_pt > 30, False))
        selection.add('dilep_pt_45'   , ak.fill_none(dilep_pt > 45, False))
        selection.add('dilep_pt_50'   , ak.fill_none(dilep_pt > 50, False))
        selection.add('dilep_pt_60'   , ak.fill_none(dilep_pt > 60, False))
        selection.add('dilep_m'    , ak.fill_none(np.abs(dilep_m - self.zmass) < 15, False))
        selection.add('met_50'        , ak.fill_none(p4_met.pt > 50, False))
        selection.add('met_70'        , ak.fill_none(p4_met.pt > 70, False))
        selection.add('met_100'        , ak.fill_none(p4_met.pt > 100, False))
        selection.add('met_50_100'    , ak.fill_none((p4_met.pt> 50) & (p4_met.pt<100), False))
        selection.add('dphi_met_ll', ak.fill_none(np.abs(dphi_met_ll) > 0.5, False) )
        selection.add('emu_met_70'    , ak.fill_none(emu_met.pt > 70, False))
        selection.add('scalar_balance'    , ak.fill_none((scalar_balance>0.4) & (scalar_balance<1.8), False))
        selection.add('vector_balance'    , ak.fill_none((np.abs(vector_balance<0.4)), False)
        selection.add('dR_ll'    , ak.fill_none(dR_ll < 1.8, False))
        
        # 2jet and vbs related variables
        lead_jet = ak.firsts(jets)
        subl_jet = ak.firsts(jets[lead_jet.delta_r(jets)>0.01])
        third_jet = ak.firsts(jets[(lead_jet.delta_r(jets)>0.01) & (subl_jet.delta_r(jets)>0.01)])
        
        dijet_mass = (lead_jet + subl_jet).mass
        dijet_dphi = lead_jet.delta_phi(subl_jet)
        dijet_deta = np.abs(lead_jet.eta - subl_jet.eta)
        dijet_zep1 = np.abs(2*lead_lep.eta - (lead_jet.eta + subl_jet.eta))/dijet_deta
        dijet_zep2 = np.abs(2*subl_lep.eta - (lead_jet.eta + subl_jet.eta))/dijet_deta

        jmet_dphi  = lead_jet.delta_phi(event.MET)

        selection.add('dijet_mass_lower_200' , ak.fill_none(dijet_mass < 200, False))
        selection.add('dijet_mass_low' , ak.fill_none(dijet_mass > 300, False))
        selection.add('dijet_mass'     , ak.fill_none(dijet_mass > 400, False))
        selection.add('dijet_mass_bin0', ak.fill_none((dijet_mass >= 400) & (dijet_mass < 800 ), False))
        selection.add('dijet_mass_bin1', ak.fill_none((dijet_mass >= 800) & (dijet_mass < 1200), False))
        selection.add('dijet_mass_bin2', ak.fill_none((dijet_mass >= 1200), False))
        selection.add('dijet_deta'     , ak.fill_none(dijet_deta > 2.5, False))
        selection.add('jmet_dphi'      , ak.fill_none(jmet_dphi > 0.5, False))
        selection.add('delta_phi_ll_met', ak.fill_none(delta_phi_ll_met > 1.0, False))
        selection.add('delta_phi_ll_met_lower_1', ak.fill_none(delta_phi_ll_met < 1.0, False))
        
        event['lead_jet_pt'] = lead_jet.pt
        event['lead_jet_eta'] = lead_jet.eta
        event['lead_jet_phi'] = lead_jet.phi
        event['trail_jet_pt'] = subl_jet.pt
        event['trail_jet_eta'] = subl_jet.eta
        event['trail_jet_phi'] = subl_jet.phi
        event['third_jet_pt'] = third_jet.pt
        event['third_jet_eta'] = third_jet.eta
        event['third_jet_phi'] = third_jet.phi
        
        # Apply GNN
        event['gnn_score'] = applyGNN(event).get_nnscore()
        
        # Now adding weights
        if not is_data:
            weights.add('genweight', event.genWeight)
            self._btag.append_btag_sf(jets, weights)
            self._purw.append_pileup_weight(weights, event.Pileup.nPU)
            _ones = np.ones(len(weights.weight()))
            if "PSWeight" in event.fields:
                theory_ps_weight(weights, event.PSWeight)
            else:
                theory_ps_weight(weights, None)
            if "LHEPdfWeight" in event.fields:
                theory_pdf_weight(weights, event.LHEPdfWeight)
            else:
                theory_pdf_weight(weights, None)
                
            if ('LHEScaleWeight' in event.fields) and (len(event.LHEScaleWeight[0]) > 0):
                weights.add('QCDScale0w'  , _ones, event.LHEScaleWeight[:, 1], event.LHEScaleWeight[:, 7])
                weights.add('QCDScale1w'  , _ones, event.LHEScaleWeight[:, 3], event.LHEScaleWeight[:, 5])
                weights.add('QCDScale2w'  , _ones, event.LHEScaleWeight[:, 0], event.LHEScaleWeight[:, 8])
                
            if 'LHEReweightingWeight' in event.fields and 'aQGC' in dataset:
                for i in range(1057):
                    weights.add(f"eft_{self._eftnames[i]}", event.LHEReweightingWeight[:, i])
        
        # selections
        common_sel = ['triggers', 'lumimask', 'metfilter']
        dilep_sel = ['2lep', 'OSSF', 'dilep_m', '0bjets', 'dphi_met_ll', '0bjets', '0htaus']
        channels = {
            # vbs signal regions
            'catSR0_VBS': common_sel + ['2lep', 'OSSF', 'dilep_m', 'dilep_pt_60', 'met_100', 'dphi_met_ll', 'dijet_mass', 'dijet_deta', 'jmet_dphi', '0bjets', '0htaus', '0njets'],
            'catSR1_VBS': common_sel + ['2lep', 'OSSF', 'dilep_m', 'dilep_pt_60', 'met_100', 'dphi_met_ll', 'dijet_mass', 'dijet_deta', 'jmet_dphi', '0bjets', '0htaus', '1njets'],
            'catSR2_VBS': common_sel + ['2lep', 'OSSF', 'dilep_m', 'dilep_pt_60', 'met_100', 'dphi_met_ll', 'dijet_mass', 'dijet_deta', 'jmet_dphi', '0bjets', '0htaus', '2njets_inc'],

            # ZZinc signal regions
            'catSR_ZZinc' : common_sel + ['2lep', 'OSSF', 'dilep_m', 'dilep_pt_60', 'met_100', 'dphi_met_ll', '0bjets', '0htaus', '01njets'],
            'catSR0_ZZinc': common_sel + ['2lep', 'OSSF', 'dilep_m', 'dilep_pt_60', 'met_100', 'dphi_met_ll', '0bjets', '0htaus', '0njets'],
            'catSR1_ZZinc': common_sel + ['2lep', 'OSSF', 'dilep_m', 'dilep_pt_60', 'met_100', 'dphi_met_ll', '0bjets', '0htaus', '1njets'],
            
            # control regions
            'catDY_ZZinc': common_sel + ['2lep', 'OSSF', 'dilep_m', 'dilep_pt_60', 'met_50_100','delta_phi_ll_met_lower_1', '0bjets', '01njets', '0htaus','dR_ll'],
            'cat3L_ZZinc': common_sel + ['3lep', 'OSSF', 'dilep_m', 'dilep_pt_30', 'emu_met_70', 'vector_balance', '0bjets', '01njets'],
            'catEM_ZZinc': common_sel + ['2lep', 'OF', 'dilep_m', 'dilep_pt_45', 'met_70', '0bjets','01njets'],
            'catTT_ZZinc': common_sel + ['2lep', 'OF', 'dilep_m', 'dilep_pt_45', 'met_70', '1bjets_inc', '012njets'],
            'catNR_ZZinc': common_sel + ['2lep', 'OF', 'dilep_m', 'dilep_pt_45', 'met_70',],
            
            # additional selections
            'catSR_VBS': common_sel + ['2lep', 'OSSF', 'dilep_m', 'dilep_pt_60', 'met_100', 'dphi_met_ll', 'dijet_mass', 'dijet_deta', 'jmet_dphi', '0bjets', '0htaus'],
            'catDY_VBS': common_sel + ['2lep', 'OSSF', 'dilep_m', 'dilep_pt_60', 'met_50_100', '0bjets', '2njets_inc', '0htaus', 'dijet_mass_lower_200'],
            'catEM_VBS': common_sel + ['2lep', 'OF', 'dilep_m', 'dilep_pt_45', 'met_70', '0bjets', '2njets_inc'],
            'cat3L_VBS': common_sel + ['3lep', 'OSSF', 'dilep_m', 'dilep_pt_30', 'emu_met_70', '0bjets','2njets_inc'],
            'catTT_VBS': common_sel + ['2lep', 'OF', 'dilep_m', 'dilep_pt_45', '1bjets_inc', '2njets_inc', 'met_70'],
            'catNR_VBS': common_sel + ['2lep', 'OF', 'dilep_m', 'dilep_pt_45', '2njets_inc', 'met_70'],
            
        }
        
        if shift_name is None:
            systematics = [None] + list(weights.variations)
        else:
            systematics = [shift_name]
            
        def _format_variable(variable, cut):
            if cut is None:
                return ak.to_numpy(ak.fill_none(variable, np.nan))
            else:
                return ak.to_numpy(ak.fill_none(variable[cut], np.nan))
        
        def _histogram_filler(ch, syst, var, _weight=None):
            sel_ = channels[ch]
            sel_ = [s for s in sel_ if var not in s]
            cut =  selection.all(*sel_)
            systname = 'nominal' if syst is None else syst
            
            if _weight is None: 
                if syst in weights.variations:
                    weight = weights.weight(modifier=syst)[cut]
                else:
                    weight = weights.weight()[cut]
            else:
                weight = weights.weight()[cut] * _weight[cut]
            
            histos[var].fill(
                **{
                    "channel": ch, 
                    "systematic": systname, 
                    var: _format_variable(event[var], cut), 
                    "weight": weight,
                }
            )
                
            
        for ch in channels:
            cut = selection.all(*channels[ch])
            for sys in systematics:
                _histogram_filler(ch, sys, 'met')
                _histogram_filler(ch, sys, 'dilep_mt')
                _histogram_filler(ch, sys, 'njets')
                _histogram_filler(ch, sys, 'bjets')
                _histogram_filler(ch, sys, 'dphi_met_ll')
                _histogram_filler(ch, sys, 'gnn_score')
                
        return {dataset: histos}
        
    def process(self, event: processor.LazyDataFrame):
        dataset_name = event.metadata['dataset']
        is_data = event.metadata.get("is_data")
        
        if is_data:
            # HEM15/16 issue
            if self._era == "2018":
                _runid = (event.run >= 319077)
                jets = event.Jet
                j_mask = ak.where((jets.phi > -1.57) & (jets.phi < -0.87) &
                                  (jets.eta > -2.50) & (jets.eta <  1.30) & 
                                  _runid, 0.8, 1)
                met = event.MET
                #event['met_pt'] = met.pt
                #event['met_phi'] = met.phi            
                jets['pt']   = j_mask * jets.pt
                jets['mass'] = j_mask * jets.mass
                event = ak.with_field(event, jets, 'Jet')
                
                
            return self.process_shift(event, None)
        
        # x-y met shit corrections
        # for the moment I am replacing the met with the corrected met 
        # before doing the JES/JER corrections
        
        run = event.run 
        npv = event.PV.npvs
        met = event.MET
        
        met = met_phi_xy_correction(
            event.MET, run, npv, 
            is_mc=not is_data, 
            era=self._era
        )
        event = ak.with_field(event, met, 'MET')
		
        
        # JES/JER corrections
        jets = self._jmeu.corrected_jets(event.Jet, event.fixedGridRhoFastjetAll, event.caches[0])
        met  = self._jmeu.corrected_met(event.MET, jets, event.fixedGridRhoFastjetAll, event.caches[0])
         
        # Apply rochester_correction
        muon=event.Muon
        muonEnUp=event.Muon
        muonEnDown=event.Muon
        muon_pt,muon_pt_roccorUp,muon_pt_roccorDown=rochester_correction(is_data).apply_rochester_correction (muon)
        
        muon['pt'] = muon_pt
        muonEnUp['pt'] = muon_pt_roccorUp
        muonEnDown['pt'] = muon_pt_roccorDown 
        event = ak.with_field(event, muon, 'Muon')
        
        # Electron corrections
        electronEnUp=event.Electron
        electronEnDown=event.Electron

        electronEnUp  ['pt'] = event.Electron['pt'] + event.Electron.energyErr/np.cosh(event.Electron.eta)
        electronEnDown['pt'] = event.Electron['pt'] - event.Electron.energyErr/np.cosh(event.Electron.eta)
        
        print("muons  : ", ak.firsts(muon.pt    )[ak.num(muon)>0])
        print("mu   up: ", ak.firsts(muonEnUp.pt)[ak.num(muon)>0])
        print("mu down: ", ak.firsts(muonEnDown.pt)[ak.num(muon)>0])
        print(" -------------- ")
        print("electron : ", ak.firsts(event.Electron.pt)[ak.num(event.Electron)>0])
        print("      up : ", ak.firsts(electronEnUp.pt)  [ak.num(event.Electron)>0])
        print("    down : ", ak.firsts(electronEnDown.pt)[ak.num(event.Electron)>0])
        print(" -------------- ")
            
        # define all the shifts
        shifts = [
            # Jets
            ({"Jet": jets               , "MET": met               }, None     ),
            ({"Jet": jets.JES_Total.up  , "MET": met.JES_Total.up  }, "JESUp"  ),
            ({"Jet": jets.JES_Total.down, "MET": met.JES_Total.down}, "JESDown"),
            ({"Jet": jets.JER.up        , "MET": met.JER.up        }, "JERUp"  ),
            ({"Jet": jets.JER.down      , "MET": met.JER.down      }, "JERDown"),
            ({"Jet": jets, "MET": met.MET_UnclusteredEnergy.up     }, "UESUp"  ),
            ({"Jet": jets, "MET": met.MET_UnclusteredEnergy.down   }, "UESDown"), 
            # Leptons + MET shift (FIXME: shift to be added)
            ({"Electron": electronEnUp  }, "ElectronEnUp"  ),
            ({"Electron": electronEnDown}, "ElectronEnDown"),
            ({"Muon": muonEnUp  }, "MuonRocUp"),
            ({"Muon": muonEnDown}, "MuonRocDown"),
        ]
        
        shifts = [
            self.process_shift(
                update_collection(event, collections), 
                name
            ) for collections, name in shifts
        ]
        return processor.accumulate(shifts)
    
    def postprocess(self, accumulator):
        return accumulator